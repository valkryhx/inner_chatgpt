"""校验《沙箱服务化评估》文档里的源码坐标与断言。

## 为什么需要

那份文档通篇是「A 文件 B 行说的是 C」——行号错一个，读者按图索骥就会
看到完全不同的代码。而这类错**读起来完全不别扭**（AGENTS.md §8.6 说的
「自洽 ≠ 正确」）。

## 校验什么

1. hlaudz 侧的三处坐标（`_host_workspace` / `_client` / `volumes=`）
2. LibreChat 侧的三处坐标（`createCodeExecutionTool` / `getCodeBaseURL` / `/upload`）
3. 几个**否定结论**（must_not）：
   - LibreChat 的 docker-compose.yml 里没有 code interpreter 服务
   - hlaudz 的 app 容器当前没有 docker 能力

用法：
    python docs/probes/20261119-沙箱服务化评估核验.py
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# REPO 用环境变量覆盖：反向确认会把这个脚本复制到临时目录运行，
# 那时 __file__ 变了、parents[2] 跟着错。显式允许外部指定根目录。
import os

REPO = Path(os.environ.get("HLZ_ASSESS_REPO") or Path(__file__).resolve().parents[2])
BACKEND = REPO / "backend"
LIBRECHAT = Path(os.environ.get("HLZ_LIBRECHAT") or r"D:\git_codes\LibreChat")
DOC = Path(os.environ.get("HLZ_ASSESS_DOC") or (REPO / "docs" / "20261119-沙箱服务化评估.md"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        print(f"  [OK]   {name}")
    else:
        print(f"  [FAIL] {name}" + (f" — {detail}" if detail else ""))
        FAILS.append(name)


def line_of(path: Path, n: int) -> str:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"<读取失败 {exc}>"
    if 1 <= n <= len(lines):
        return lines[n - 1]
    return f"<第 {n} 行不存在，共 {len(lines)} 行>"


def must_not(name: str, cond_absent: bool, detail: str = "") -> None:
    """否定结论：必须不存在。"""
    check(f"[must_not] {name}", cond_absent, detail)


def main() -> int:
    print("=" * 70)
    print("一、hlaudz 侧坐标")
    print("=" * 70)

    l = line_of(BACKEND / "app" / "sandbox" / "manager.py", 72)
    check("manager.py:72 是 _host_workspace", l.startswith("def _host_workspace"), l[:80])

    l = line_of(BACKEND / "app" / "sandbox" / "runtime.py", 65)
    check("runtime.py:65 是 _client", l.startswith("def _client"), l[:80])

    l = line_of(BACKEND / "app" / "sandbox" / "runtime.py", 133)
    check("runtime.py:133 是 volumes=", "volumes=" in l, l[:80])

    # _host_workspace 必须真的反向依赖 write_root_for（这是文档的核心论据）
    src = (BACKEND / "app" / "sandbox" / "manager.py").read_text(encoding="utf-8")
    check("manager.py 真的 import 了 write_root_for",
          "from ..workspace import write_root_for" in src)

    # _client 必须真的用 docker.from_env
    src = (BACKEND / "app" / "sandbox" / "runtime.py").read_text(encoding="utf-8")
    check("runtime.py 真的调用 docker.from_env()", "docker.from_env()" in src)

    print()
    print("=" * 70)
    print("二、LibreChat 侧坐标")
    print("=" * 70)

    if not LIBRECHAT.exists():
        print(f"  [SKIP] {LIBRECHAT} 不存在，跳过 LibreChat 侧校验")
    else:
        l = line_of(LIBRECHAT / "api/app/clients/tools/util/handleTools.js", 346)
        check("handleTools.js:346 是 createCodeExecutionTool",
              "createCodeExecutionTool" in l, l[:80])

        l = line_of(LIBRECHAT / "api/server/services/Files/Code/crud.js", 135)
        check("crud.js:135 是 getCodeBaseURL",
              "getCodeBaseURL" in l, l[:80])

        l = line_of(LIBRECHAT / "api/server/services/Files/Code/crud.js", 156)
        check("crud.js:156 是 POST /upload",
              "/upload" in l and "post" in l.lower(), l[:80])

        # .env.example 里必须有这几个键
        env = (LIBRECHAT / ".env.example").read_text(encoding="utf-8", errors="replace")
        for key in ("LIBRECHAT_CODE_BASEURL", "CODEAPI_JWT_ENABLED",
                    "CODEAPI_TENANT_ISOLATION_STRICT", "CODE_SANDBOX_PREWARM",
                    "CODE_SANDBOX_COLD_AFTER_MS"):
            check(f".env.example 含 {key}", key in env)

        print()
        print("=" * 70)
        print("三、否定结论（must_not）")
        print("=" * 70)

        compose = (LIBRECHAT / "docker-compose.yml").read_text(encoding="utf-8", errors="replace")
        # 只看服务定义行（行首两空格 + 名字 + 冒号）
        services = re.findall(r"^  ([a-z][a-z0-9-]*):\s*$", compose, re.M)
        has_ci = any("interpreter" in s or "sandbox" in s for s in services)
        must_not("LibreChat 主 compose 里没有 code interpreter / sandbox 服务",
                 not has_ci, f"实际服务: {services}")

    print()
    print("=" * 70)
    print("四、文档自身")
    print("=" * 70)

    doc = DOC.read_text(encoding="utf-8")
    check("文档存在且非空", len(doc) > 5000, f"{len(doc)} 字符")
    check("文档标注了「未经运行时验证」", "未经运行时验证" in doc)
    check("文档区分了实测与推断",
          "实测" in doc and "推断" in doc)
    # 不能有已被修正的旧行号
    must_not("文档里没有 runtime.py:124 这个旧坐标", "runtime.py:124" not in doc)
    check("文档用的是修正后的 runtime.py:133", "runtime.py:133" in doc)

    print()
    print("=" * 70)
    print("五、§7 运行时选型章节")
    print("=" * 70)

    for sec in ("## 7. 运行时选型", "### 7.4", "### 7.5", "### 7.6"):
        check(f"§7 章节存在：{sec}", sec in doc)

    # §7 必须**显式**标注自己是实测。
    #
    # 不能只靠上面那条「文档区分了实测与推断」——那条是全文级判定，
    # 别处有「实测」二字就绿。实测发现：把 §7 的实测标注删掉，全文级
    # 断言仍然绿（假绿）。所以 §7 要单独钉死。
    check("§7 显式标注自己是实测",
          "第 7 章（运行时选型）**是实测**" in doc)
    # §7 也必须显式交代它没覆盖什么（防「实测」被读成「生产已验证」）
    check("§7 显式标注未被生产验证",
          "所有验证都在「容器内跑 bwrap」" in doc)

    # 五个坑必须都在（漏一个就说明实测结论没落实）
    for pit in ("`--ro-bind / /` 会把 app 的 `.env`",
                "会让 `/etc` 变可写",
                "--clearenv",
                "假降权",
                "只管单进程"):
        check(f"坑已记录：{pit}", pit in doc)

    # 探针脚本必须存在且真的包含对应断言
    probe = REPO / "docs" / "probes" / "20261119-bwrap可行性实测.sh"
    check("bwrap 探针脚本存在", probe.exists(), str(probe))
    if probe.exists():
        src = probe.read_text(encoding="utf-8", errors="replace")
        # 用 `ok <编号> ` 带尾随空格匹配，避免 "ok 10b" 命中 "ok 10b-REMOVED"
        # 这类子串陷阱（反向确认第二组抓出来的）
        for n in ("ok 10b ", "ok 12 ", "ok 14 ", "ok 22 ", "ok 21 "):
            check(f"探针含断言 {n.strip()}", n in src)
        # 关键：断言必须只看退出码，不能比对 mount 的输出文本
        #
        # ⚠️ 两个坑都在这里踩过：
        #  1. must_not 的语义是「条件成立 = 通过」，所以要传 `not in`。
        #     写反了是**静默**的：「坏写法存在」会被当成通过。
        #  2. 不能直接对全文做纯文本匹配 —— 探针的**注释**里就合法地
        #     写着这个坏写法（记录踩过的坑），纯文本匹配会把注释算进去，
        #     于是对照用例永远红。必须先剥掉注释行再查代码。
        #     （AGENTS.md §8.5「静态守卫的正确写法」同一类问题）
        code_only = "\n".join(
            ln for ln in src.splitlines()
            if not ln.lstrip().startswith("#"))
        must_not("探针不再用『输出文本比对』判 mount 是否被拒",
                 "echo denied" not in code_only)
        check("探针用退出码判 mount / chroot",
              "mount rc=" in src or 'echo $?' in src)

    # 探针不能把**探针脚本目录**放到 Git Bash 的 /tmp：
    # Docker Desktop 解析不了这个 POSIX 路径，会把脚本挂成一个空目录 ——
    # 探针一行不跑却输出格式正确的空表（本项目实际踩过这个假信号）。
    #
    # ⚠️ 只禁「用 mktemp 当 -v 挂载源」这一种用法，不是禁 mktemp 本身。
    # `docker build <ctx>` 用 mktemp 是可以的（实测通过）——build context 由
    # docker CLI 自己读，不走 daemon 的路径解析。断言写宽了会误伤。
    if probe.exists():
        src2 = probe.read_text(encoding="utf-8", errors="replace")
        mount_from_probe_dir = re.findall(r'-v\s+"\$PROBE/', src2)
        must_not("探针不用 $PROBE 作 -v 挂载源（须用 Windows 路径）",
                 not mount_from_probe_dir,
                 f"发现 {len(mount_from_probe_dir)} 处")
        check("探针把挂载路径转成 Windows 形式", "cygpath -w" in src2)

    print()
    print("=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())