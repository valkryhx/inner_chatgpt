#!/bin/sh
# bwrap 作为沙箱运行时的可行性实测（一键复现）
#
# 用法（Git Bash）：
#   sh docs/probes/20261119-bwrap可行性实测.sh
#
# 前置：本机有 docker。脚本会：
#   1. 构建一个带 bubblewrap 的探针镜像（基于 hlzscale-app:latest）
#   2. 在容器内跑 16 项实测
#   3. 输出 markdown 表格
#
# 为什么要在容器里测，而不是直接在宿主机测：
#   真实部署形态是「app 跑在容器里，沙箱是它的子进程」。宿主机上 bwrap 能跑
#   不代表容器里能跑 —— Docker 默认 seccomp 会拦一部分 unshare 调用。
#
# 为什么 --user 1000 要单独测一轮：
#   RLIMIT_NPROC 对 root 静默失效（实测），只有非 root 才拦得住 fork 炸弹。
#   这一条决定了 app 容器必须跑非 root，是选型的关键约束。
set -u

REPO_DIR=$(cd "$(dirname "$0")/../.." && pwd)
IMAGE=hlz-bwrap-probe:20261119
BASE=${BWRAP_BASE_IMAGE:-hlzscale-app:latest}

say() { printf '%s\n' "$*"; }
hr()  { say "----------------------------------------------------------------------"; }

say "================================================================"
say " bwrap 可行性实测"
say "================================================================"
say "  base image : $BASE"
say "  probe image: $IMAGE"

# ---- 构建探针镜像 ----------------------------------------------------------
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  say ""
  say "构建探针镜像（首次约 30s）..."
  CTX=$(mktemp -d)
  cat > "$CTX/Dockerfile" <<'HLZ_DOCKERFILE_BWRAP_20261119'
ARG BASE
FROM ${BASE}
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends bubblewrap util-linux \
    && rm -rf /var/lib/apt/lists/*
HLZ_DOCKERFILE_BWRAP_20261119
  docker build -q --build-arg BASE="$BASE" -t "$IMAGE" "$CTX" >/dev/null || {
    say "!! 构建失败"; rm -rf "$CTX"; exit 1; }
  rm -rf "$CTX"
fi

# 探针脚本必须放在**仓库内**，不能用 mktemp。
#
# 实测（2026-11-19）：Git Bash 的 mktemp -d 给出 /tmp/tmp.XXXX，这个 POSIX 路径
# 传给 docker -v 时 Docker Desktop 解析不了，它会**建出一个同名目录**挂进去，
# 于是容器里 `sh /probe.sh` 报 "Is a directory" 或者静默什么都不做 ——
# 探针一行都没跑，输出却是"格式正确"的空表，最难发现的假信号。
# 用仓库内路径 + cygpath -w 转 Windows 形式，才稳定。
PROBE="$REPO_DIR/work/bwrap-probe"
mkdir -p "$PROBE"
# Docker Desktop 接受 D:/... 形式；Git Bash 的 /d/... 有时也能用但不稳
PROBE_WIN=$(cygpath -w "$PROBE" 2>/dev/null || printf '%s' "$PROBE")
PROBE_WIN=$(printf '%s' "$PROBE_WIN" | sed 's|\\|/|g')

# ===========================================================================
# 探针脚本 1：以 root 跑（大多数部署的默认形态）
# ===========================================================================
cat > "$PROBE/root.sh" <<'HLZ_PROBE_ROOT_20261119'
set -u
ROOTPASS=0; ROOTFAIL=0
ok()   { printf '| %-2s | %-38s | %s |\n' "$1" "$2" "$3"; ROOTPASS=$((ROOTPASS+1)); }
bad()  { printf '| %-2s | %-38s | **%s** |\n' "$1" "$2" "$3"; ROOTFAIL=$((ROOTFAIL+1)); }

echo '| # | 验证项 | 结果 |'
echo '|---|---|---|'

# 1. bwrap 存在
V=$(bwrap --version 2>&1) && ok 1 "bwrap 可安装" "$V" || bad 1 "bwrap 可安装" "缺失"

# 2. userns 可用
if bwrap --unshare-all --ro-bind /usr /usr --ro-bind /lib /lib \
        --ro-bind /lib64 /lib64 --ro-bind /bin /bin \
        --proc /proc --dev /dev --tmpfs /tmp /bin/true 2>/dev/null; then
  ok 2 "Docker 默认 seccomp 下可建 userns" "rc=0"
else
  bad 2 "Docker 默认 seccomp 下可建 userns" "EPERM"
fi

# 3. 拿到几个 namespace
NS=$(bwrap --unshare-all --ro-bind / / --proc /proc --dev /dev --tmpfs /tmp \
       /bin/sh -c 'ls /proc/self/ns | tr "\n" " "' 2>/dev/null)
ok 3 "拿到的 namespace" "$NS"

# 4. 不加 --unshare 会失败（说明容器无 CAP_SYS_ADMIN）
if bwrap --ro-bind / / --tmpfs /tmp /bin/true 2>/dev/null; then
  bad 4 "不加 --unshare 时失败" "居然成功（容器有 SYS_ADMIN？）"
else
  ok 4 "不加 --unshare 时失败" "EPERM（容器无 CAP_SYS_ADMIN）"
fi

mkdir -p /ws/u1
BW='bwrap --unshare-all --clearenv --cap-drop ALL
    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME /tmp
    --setenv LANG C.UTF-8 --setenv MPLCONFIGDIR /tmp/.mpl
    --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64
    --ro-bind /bin /bin --ro-bind /sbin /sbin
    --ro-bind /etc/fonts /etc/fonts
    --bind /ws/u1 /ws --tmpfs /tmp --proc /proc --dev /dev --chdir /ws'

# 5. 网络断开
OUT=$($BW python3 -c '
import socket
try:
    socket.create_connection(("1.1.1.1", 443), timeout=3); print("连上了")
except OSError as e: print(type(e).__name__)' 2>&1)
case "$OUT" in *Error*|*Timeout*) ok 5 "网络断开" "$OUT";; *) bad 5 "网络断开" "$OUT";; esac

# 6. 最小挂载集的根目录
OUT=$($BW /bin/sh -c 'ls / | tr "\n" " "' 2>&1)
ok 6 "最小挂载集下 ls /" "$OUT"

# 7. cap-drop ALL
OUT=$($BW /bin/sh -c 'grep ^CapEff /proc/self/status' 2>&1 | tr -d ' \t')
case "$OUT" in *0000000000000000*) ok 7 "cap-drop ALL 后 CapEff" "全零";; *) bad 7 "cap-drop ALL 后 CapEff" "$OUT";; esac

# 8. NoNewPrivs
OUT=$($BW /bin/sh -c 'grep ^NoNewPrivs /proc/self/status' 2>&1 | tr -d ' \t')
case "$OUT" in *NoNewPrivs:1*) ok 8 "NoNewPrivs" "1";; *) bad 8 "NoNewPrivs" "$OUT";; esac

# 9. mount / chroot 被拒
#
# ⚠️ 断言必须只取 rc，不能比对输出文本。
# 实测第一版写成 `mount ... 2>&1 && echo ok || echo denied`，output 里
# 混进了 "dmesg(1) may have more information..." 这一行，字符串比较失败，
# 于是把一个**正确的拒绝**报成了异常（假红）。判「是否被拒」只看退出码。
M=$($BW /bin/sh -c 'mount -t tmpfs none /mnt 2>/dev/null; echo $?' 2>/dev/null)
C=$($BW /bin/sh -c 'chroot / /bin/true 2>/dev/null; echo $?' 2>/dev/null)
if [ "$M" != 0 ] && [ "$C" != 0 ]; then
  ok 9 "mount / chroot 被拒" "mount rc=$M chroot rc=$C"
else
  bad 9 "mount / chroot 被拒" "mount rc=$M chroot rc=$C"
fi

# 10. 写入边界
#
# ⚠️ 这里藏着一个 bwrap 的实际坑（本探针第一次跑就抓到了）：
#     `--ro-bind /etc/fonts /etc/fonts` 时，bwrap 会为挂载点**自动创建父目录
#     /etc**，而且这个自动创建的父目录是**可写**的。于是 `echo x > /etc/hosts`
#     会成功 —— /etc 下凭空出现一个 hosts 文件。
#     它不是逃逸（写入只存在于本次 mnt namespace，新沙箱里没有），但同一沙箱
#     内派生的子进程会继承，足以写 /etc/ld.so.preload 影响自己后续起的进程。
#     正确写法是先 `--tmpfs /etc` 占位，再往上面叠需要的分支。
W=$($BW /bin/sh -c 'echo x > /ws/a.txt && echo ws-ok' 2>/dev/null)
U=$($BW /bin/sh -c 'echo x > /usr/x.txt 2>/dev/null; echo $?' 2>/dev/null)
if [ "$W" = ws-ok ] && [ "$U" != 0 ]; then
  ok 10a "写入边界：/ws 可写、/usr 只读" "ok"
else
  bad 10a "写入边界：/ws 可写、/usr 只读" "ws=$W usr_rc=$U"
fi
# 10b. 未加 --tmpfs /etc 时 /etc 意外可写（如实记录，不是期望行为）
ETC_RAW=$(bwrap --unshare-all --clearenv --cap-drop ALL \
  --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin --ro-bind /sbin /sbin --ro-bind /etc/fonts /etc/fonts \
  --tmpfs /tmp --proc /proc --dev /dev \
  /bin/sh -c 'echo x > /etc/hosts 2>/dev/null; echo $?' 2>/dev/null)
case "$ETC_RAW" in
  0) ok 10b "坑：--ro-bind /etc/fonts 会让 /etc 变可写" "确认（须加 --tmpfs /etc）";;
  *) ok 10b "坑：--ro-bind /etc/fonts 会让 /etc 变可写" "本次未复现 rc=$ETC_RAW";;
esac
# 10c. 加了 --tmpfs /etc 之后 /etc 里只有显式挂进去的东西
ETC_FIX=$(bwrap --unshare-all --clearenv --cap-drop ALL \
  --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME /tmp \
  --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin --ro-bind /sbin /sbin \
  --tmpfs /etc --ro-bind /etc/fonts /etc/fonts \
  --tmpfs /tmp --proc /proc --dev /dev \
  /bin/sh -c 'ls /etc | tr "\n" " "' 2>/dev/null)
case "$ETC_FIX" in
  "fonts "*) ok 10c "--tmpfs /etc 后 /etc 只剩显式项" "$ETC_FIX";;
  *) bad 10c "--tmpfs /etc 后 /etc 只剩显式项" "$ETC_FIX";;
esac
# 10d. /etc 里的写入是否持久（跨沙箱）
bwrap --unshare-all --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin --ro-bind /sbin /sbin --ro-bind /etc/fonts /etc/fonts \
  --tmpfs /tmp --proc /proc --dev /dev \
  /bin/sh -c 'echo persistent > /etc/marker.txt' 2>/dev/null
PERSIST=$(bwrap --unshare-all --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64 \
  --ro-bind /bin /bin --ro-bind /sbin /sbin --ro-bind /etc/fonts /etc/fonts \
  --tmpfs /tmp --proc /proc --dev /dev \
  /bin/sh -c 'cat /etc/marker.txt 2>/dev/null || echo absent' 2>/dev/null)
case "$PERSIST" in
  absent) ok 10d "/etc 写入不跨沙箱持久" "absent（只存活于本次 mnt ns）";;
  *) bad 10d "/etc 写入不跨沙箱持久" "$PERSIST";;
esac

# 11. 环境变量默认是否泄漏（关键：不 --clearenv 就会漏）
LEAK=$(bwrap --unshare-all --ro-bind / / --tmpfs /tmp --proc /proc --dev /dev \
        /bin/sh -c 'echo "[$HLZ_TEST_SECRET]"' 2>&1)
if [ "$LEAK" = "[s3cr3t]" ]; then
  ok 11 "不加 --clearenv 时环境变量泄漏" "泄漏（必须显式 --clearenv）"
else
  bad 11 "不加 --clearenv 时环境变量泄漏" "未泄漏？$LEAK"
fi
CLEAN=$($BW /bin/sh -c 'echo "[$HLZ_TEST_SECRET]"' 2>&1)
case "$CLEAN" in '[]'*) ok 12 "--clearenv 后密钥不再泄漏" "空";; *) bad 12 "--clearenv 后密钥不再泄漏" "$CLEAN";; esac

# 13. uid 是假降权
OUT=$(bwrap --unshare-all --uid 1000 --gid 1000 --ro-bind / / --proc /proc \
       --dev /dev --tmpfs /tmp /bin/sh -c 'cat /proc/self/uid_map' 2>&1 | tr -s ' ')
ok 13 "bwrap --uid 1000 的 uid_map" "$OUT（内核视角 uid 仍为 0）"

# 14. RLIMIT_NPROC 在 root 下是否失效
OUT=$($BW prlimit --nproc=20 python3 -c '
import subprocess
n = 0
try:
    for _ in range(60):
        subprocess.Popen(["sleep", "2"]); n += 1
except OSError: pass
print(n)' 2>&1)
if [ "$OUT" -ge 55 ] 2>/dev/null; then
  ok 14 "RLIMIT_NPROC=20 在 root 下" "失效（起了 $OUT 个）"
else
  bad 14 "RLIMIT_NPROC=20 在 root 下" "起了 $OUT 个（本想看到失效）"
fi

# 15. 进程可见性
PIDS=$($BW /bin/sh -c 'ls /proc | grep -cE "^[0-9]+$"' 2>&1)
REAL=$(ls /proc | grep -cE '^[0-9]+$')
ok 15 "沙箱内可见进程 / 容器内真实" "$PIDS / $REAL"

# 16. 启动开销
N=50
T0=$(date +%s%N)
i=0; while [ $i -lt $N ]; do
  bwrap --unshare-all --ro-bind / / --tmpfs /tmp /bin/true 2>/dev/null; i=$((i+1))
done
T1=$(date +%s%N)
ok 16 "启动开销（${N} 次平均）" "$(( (T1-T0)/1000000/N )) ms"

# 17. app 源码在 --ro-bind / / 下可读（这是坑）
mkdir -p /fakeapp 2>/dev/null
echo 'DB_PASSWORD=leak' > /fakeapp/.env 2>/dev/null
OUT=$(bwrap --unshare-all --ro-bind / / --tmpfs /tmp --proc /proc --dev /dev \
      /bin/sh -c 'cat /fakeapp/.env 2>/dev/null || echo unavailable' 2>&1)
case "$OUT" in *DB_PASSWORD*) ok 17 "--ro-bind / / 暴露 app 的 .env" "可读（必须用最小挂载集）";;
  *) ok 17 "--ro-bind / / 暴露 app 的 .env" "本次不可读（取决于目录权限）";; esac
OUT=$($BW /bin/sh -c 'cat /fakeapp/.env 2>/dev/null || echo unavailable' 2>&1)
case "$OUT" in unavailable) ok 18 "最小挂载集挡住 .env" "读不到";; *) bad 18 "最小挂载集挡住 .env" "$OUT";; esac

# 19. 跨租户（bind 整个 /ws 会泄漏）
mkdir -p /ws/userA /ws/userB
echo A > /ws/userA/s.txt; echo B > /ws/userB/s.txt
OUT=$(bwrap --unshare-all --ro-bind / / --bind /ws /ws --tmpfs /tmp --proc /proc --dev /dev \
      /bin/sh -c 'cat /ws/userB/s.txt 2>/dev/null || echo blocked' 2>&1)
case "$OUT" in B) ok 19 "bind 整个 /ws 会跨租户泄漏" "读到了别人的";; *) ok 19 "bind 整个 /ws 会跨租户泄漏" "本次未泄漏";; esac
OUT=$(bwrap --unshare-all --ro-bind / / --bind /ws/userA /ws --tmpfs /tmp --proc /proc --dev /dev \
      /bin/sh -c 'cat /ws/userB/s.txt 2>/dev/null || echo blocked' 2>&1)
case "$OUT" in blocked) ok 20 "只 bind 自己目录时不泄漏" "读不到";; *) bad 20 "只 bind 自己目录时不泄漏" "$OUT";; esac

# 21. 中文字体（最小挂载集 + /etc/fonts）
OUT=$($BW python3 -c '
from matplotlib import font_manager
n = {f.name for f in font_manager.fontManager.ttflist}
cjk = sorted(x for x in n if "CJK" in x)
print(f"可见 {len(n)} 个字体，CJK: {cjk[:2]}")' 2>&1)
case "$OUT" in *CJK*|*Noto*) ok 21 "最小挂载集下中文字体可用" "$OUT";; *) bad 21 "最小挂载集下中文字体可用" "$OUT";; esac

# 22. RLIMIT_AS 是 per-process，不是总量限制（这是 bwrap 的实质缺陷）
#
# bwrap 自己**不做任何资源限制**（`bwrap --help` 里没有 mem/cpu/pids 相关参数）。
# 只能靠外挂 prlimit。但 prlimit 设的是**每个进程**的 rlimit，不是 cgroup 的组总量。
# 于是 NPROC × AS 才是实际峰值：NPROC=20 × AS=500M ⇒ 理论上限 10 GB。
# 防得住「单个进程吃爆内存」，防不住「fork 一批每个吃一点」。
# 真正要组总量得上 cgroup v2（memory.max）——那又回到「要么 docker、要么自己写 cgroup」。
OUT=$($BW prlimit --as=500000000 python3 -c '
import subprocess, sys
parent = bytearray(300*1024*1024)
code = "b = bytearray(300*1024*1024)"
ok = 0
for _ in range(4):
    if subprocess.run([sys.executable, "-c", code], capture_output=True).returncode == 0:
        ok += 1
print(ok)' 2>&1 | tail -1)
if [ "$OUT" = 4 ] 2>/dev/null; then
  ok 22 "RLIMIT_AS 是 per-process（非组总量）" "父 300MB + 4×300MB 全过（限 500MB）"
else
  bad 22 "RLIMIT_AS 是 per-process（非组总量）" "子进程成功 $OUT/4（本想 4/4）"
fi

echo
echo "小结：$ROOTPASS 项通过，$ROOTFAIL 项异常"
HLZ_PROBE_ROOT_20261119

# ===========================================================================
# 探针脚本 2：以 uid 1000 跑（验证 RLIMIT_NPROC 是否生效）
# ===========================================================================
cat > "$PROBE/nonroot.sh" <<'HLZ_PROBE_NONROOT_20261119'
set -u
echo '| # | 验证项 | 结果 |'
echo '|---|---|---|'
mkdir -p /tmp/ws 2>/dev/null || true
BW='bwrap --unshare-all --clearenv --cap-drop ALL
    --setenv PATH /usr/local/bin:/usr/bin:/bin --setenv HOME /tmp
    --ro-bind /usr /usr --ro-bind /lib /lib --ro-bind /lib64 /lib64
    --ro-bind /bin /bin --ro-bind /sbin /sbin
    --bind /tmp/ws /ws --tmpfs /tmp --proc /proc --dev /dev --chdir /ws'

OUT=$($BW prlimit --nproc=20 python3 -c '
import subprocess
n = 0
try:
    for _ in range(60):
        subprocess.Popen(["sleep", "2"]); n += 1
except OSError: pass
print(n)' 2>&1)
if [ "$OUT" -lt 40 ] 2>/dev/null; then
  printf '| %-2s | %-38s | %s |\n' N1 "RLIMIT_NPROC=20 在非 root 下" "生效（拦在第 $OUT 个）"
else
  printf '| %-2s | %-38s | **%s** |\n' N1 "RLIMIT_NPROC=20 在非 root 下" "失效（起了 $OUT 个）"
fi

OUT=$($BW prlimit --as=200000000 python3 -c '
try:
    bytearray(400*1024*1024); print("分配成功")
except MemoryError: print("MemoryError")' 2>&1)
case "$OUT" in *MemoryError*) printf '| %-2s | %-38s | %s |\n' N2 "RLIMIT_AS=200M 拦 400MB 分配" "MemoryError";;
  *) printf '| %-2s | %-38s | **%s** |\n' N2 "RLIMIT_AS=200M 拦 400MB 分配" "$OUT";; esac

OUT=$($BW prlimit --cpu=2 python3 -c '
import time
t=time.time()
while time.time()-t < 10: pass
print("跑完了")' 2>&1; echo "rc=$?")
case "$OUT" in *rc=137*|*Killed*) printf '| %-2s | %-38s | %s |\n' N3 "RLIMIT_CPU=2 杀超时进程" "Killed rc=137";;
  *) printf '| %-2s | %-38s | **%s** |\n' N3 "RLIMIT_CPU=2 杀超时进程" "$OUT";; esac
HLZ_PROBE_NONROOT_20261119

# ===========================================================================
# 执行
# ===========================================================================
say ""
say "=== 第一轮：容器以 root 跑（默认形态）==="
HLZ_TEST_SECRET=s3cr3t MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$PROBE_WIN/root.sh:/probe.sh:ro" \
  -e HLZ_TEST_SECRET=s3cr3t \
  --entrypoint sh "$IMAGE" -c 'sh /probe.sh' 2>&1

say ""
say "=== 第二轮：容器以 uid 1000 跑（验证 RLIMIT_NPROC）==="
MSYS_NO_PATHCONV=1 docker run --rm --user 1000:1000 \
  -v "$PROBE_WIN/nonroot.sh:/probe.sh:ro" \
  --entrypoint sh "$IMAGE" -c 'sh /probe.sh' 2>&1

say ""
hr
say "说明"
hr
say "  · 本次所有结论都是**本机实测**（Docker Desktop 4.37.1 / Engine 27.4.0）"
say "  · 换机器要重跑；Linux 原生内核 + 不同 seccomp profile 可能结果不同"
say "  · 探针镜像可用 BWRAP_BASE_IMAGE 换成任意基于 Debian 的镜像"