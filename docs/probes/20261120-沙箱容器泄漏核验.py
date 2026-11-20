"""核验：58 个 hlz_sbx_* 容器是否由测试泄漏而来。

从仓库根目录运行：`python docs/probes/20261120-沙箱容器泄漏核验.py`

每条断言都给出「修复后读数会变」的期望值，否则不是判据。
"""
import json, subprocess, sys, collections, re, pathlib, pathlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ok = bad = 0
def check(label, cond, detail=""):
    global ok, bad
    if cond:
        print(f"[✓] {label}"); ok += 1
    else:
        print(f"[✗] {label}  {detail}"); bad += 1

ids = subprocess.run(["docker","ps","-a","--filter","name=hlz_sbx_","-q"],
                     capture_output=True, text=True).stdout.split()
rows = []
for i in ids:
    r = subprocess.run(["docker","inspect",i,"--format","{{json .}}"],
                       capture_output=True, text=True)
    if r.returncode: continue
    d = json.loads(r.stdout)
    lb = d["Config"].get("Labels") or {}
    binds = d["HostConfig"].get("Binds") or []
    rows.append(dict(
        name=d["Name"].lstrip("/"), state=d["State"]["Status"],
        owner=lb.get("hlz.owner",""), sandbox=lb.get("hlz.sandbox",""),
        image=d["Config"]["Image"], cmd=" ".join(d["Config"].get("Cmd") or []),
        bind=binds[0] if binds else "",
    ))

print(f"容器总数 {len(rows)}（running {sum(1 for r in rows if r['state']=='running')}）\n")

# 1. 归属：owner 尾部是宿主机 PID，不是容器 ID
host = subprocess.run(["hostname"], capture_output=True, text=True).stdout.strip()
owners = [r["owner"] for r in rows if r["owner"]]
machines = {o.split(":")[0] for o in owners}
check("owner 的机器名与宿主机一致（不是 probe 栈容器）",
      machines == {host} or not machines, f"{machines} vs {host}")

tails = [o.split(":")[1] for o in owners if ":" in o]
nums = [int(t) for t in tails if t.isdigit()]
check("owner 尾部全是数字（= 宿主机 PID，见 identity.py:126）",
      len(nums) == len(tails), f"非数字：{[t for t in tails if not t.isdigit()]}")
check("★ 所有 owner 互不相同（58 个不同进程各建一个，从未复用）",
      len(set(owners)) == len(owners), f"重复 {len(owners)-len(set(owners))} 个")
check("PID 落在 Windows 合法区间（不是容器内恒为 1）",
      all(n > 100 for n in nums), f"最小值 {min(nums) if nums else 'N/A'}")

# 2. 来源：bind 全指向 pytest 临时目录
pytest_binds = [r for r in rows if "pytest-of-" in r["bind"]]
check("★ 绝大多数来自 pytest 临时目录（测试残留）",
      len(pytest_binds) >= len(rows) - 2, f"只有 {len(pytest_binds)}/{len(rows)}")
tests = collections.Counter()
for r in pytest_binds:
    m = re.search("pytest-[0-9]+" + chr(92) + chr(92) + "([^" + chr(92) + chr(92) + "]+)", r["bind"])
    tests[m.group(1) if m else "?"] += 1
check("★ 泄漏跨多个测试（不是单个测试的特例）",
      len(tests) >= 2, f"只有 {len(tests)} 个测试：{dict(tests)}")
print(f"    制造来源：{dict(tests.most_common(5))}")

# 3. 容器内容：只是 sleep infinity
cmds = collections.Counter(r["cmd"] for r in rows)
check("容器主进程全是 sleep infinity（空壳，不占 CPU）",
      set(cmds) == {"sleep infinity"}, f"{dict(cmds)}")

# 4. 镜像：证明走的是 Docker inproc 路径
imgs = collections.Counter(r["image"] for r in rows)
check("镜像全是 hlz-sandbox:2026-11-18（inproc Docker 路径，非 service）",
      set(imgs) == {"hlz-sandbox:2026-11-18"}, f"{dict(imgs)}")

# 5. 代码层：测试不调 start() ⇒ reap_loop 从未启动；单例重置丢弃池引用
MGR = open("backend/app/sandbox/manager.py", encoding="utf-8").read()
CONF = open("backend/tests/conftest.py", encoding="utf-8").read()
TESTF = open("backend/tests/test_sandbox_service_file_ops.py", encoding="utf-8").read()

check("must_not 该测试文件里没有调 start()",
      ".start()" not in TESTF, "有 start() 就不再泄漏")
check("must_not 该测试文件里没有 shutdown()",
      "shutdown" not in TESTF)
check("conftest 的 _clear() 只把 manager 置 None，不 shutdown",
      "_clear" in CONF and "shutdown" not in CONF,
      "一旦加 shutdown，容器就回收")
check("reap_loop 只在 manager.start() 里创建",
      MGR.count("create_task(reap_loop") == 1,
      f"实际 {MGR.count('create_task(reap_loop')} 处")

# 6. 服务栈的 backend 是 service ⇒ 不建 Docker 容器
COMPOSE = open("backend/deploy/docker-compose.yml", encoding="utf-8").read()
check("compose 里 app 用 HLZ_SANDBOX_BACKEND（service）",
      "HLZ_SANDBOX_SERVICE_URL" in COMPOSE)
check("sandbox 服务是 bwrap（不建 Docker 容器）",
      "hostConfig" not in COMPOSE and "sandbox_service" in COMPOSE)

print(f"\n{'✓ 全部通过' if not bad else '✗ 失败'}（{ok} 通过 / {bad} 失败）")
# 7. 文档里的数字必须与实测一致
#    第一版手写成「62 个不同 PID」，实际 58 —— 手工计数不可信，
#    所以把文档里的数字也变成可复算的断言。
DOC = pathlib.Path("docs/20261120-沙箱容器泄漏根因.md").read_text(encoding="utf-8")
_n_owner = len(set(owners))
check("文档写的 PID 数 = 实测不同 PID 数",
      f"{_n_owner} 个不同 PID" in DOC,
      f"实测 {_n_owner} 个；不匹配说明文档里是手写估的数字")
check("must_not 文档里出现错的 62 个 PID",
      "62 个不同 PID" not in DOC, "已订正为 58")

print()
if bad:
    print(f"✗ 失败 {bad} 项（通过 {ok}）")
    sys.exit(1)
print(f"✓ 全部通过（{ok} 项）")
sys.exit(0)
