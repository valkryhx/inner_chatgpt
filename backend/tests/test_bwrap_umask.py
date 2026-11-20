"""bwrap 子进程的 umask 必须是 002 —— 否则产物组不可写。

## 为什么这是 workspace 契约的一部分

workspace 由 **两个 uid** 共同写：app（root）与沙箱服务（uid 1000），
靠共享组 `hlz` 打通（见 `test_shared_group_permissions.py`）。

`ensure_dir_shared` 保证了「app 或服务建的目录」有 `g+w` + setgid。
但 **`code_run` 里跑的用户代码是第三个创建者** —— 它不受
`ensure_dir_shared` 管辖，只受进程 umask 影响。默认 022 下：

    mkdir      → 0o2755   ← setgid 从父目录继承，但组不可写
    open(...)  → 0o100644 ← 组不可写

实测（2026-11-20 真机）：修复前正是这两个值。

## 后果为什么不容易发现

用户自己写的文件，他自己是属主，能改能删 —— 表面上一切正常。
hx 的现场就是这样：`file_write` 507 之后他改用 `code_run` 写，
「文件正常生成」，于是以为问题解决了。

真正的代价在后面：workspace 的其余部分（`file_patch`、淘汰归档、
以及任何换 uid 的操作）默认这些路径对组可写。一条不满足契约的路径
会在某次看似无关的操作里炸出来。

## 为什么两条执行路径都要设

daemon 模式（预热常驻）与一次性模式（无预热）是**两段独立代码**。
只设一条会导致「预热成功的用户正常、预热失败的组不可写」这种
随负载变化的不一致 —— 比两边都错更难查。
"""
from __future__ import annotations

import re
from pathlib import Path

BWRAP = Path(__file__).resolve().parents[1] / "sandbox_service" / "executor" / "bwrap.py"


def test_umask_is_set_to_002():
    """必须显式 `os.umask(0o002)` —— 不能靠部署环境的默认值。"""
    src = BWRAP.read_text(encoding="utf-8")
    assert re.search(r"umask\(\s*0o0*2\s*\)", src), (
        "bwrap.py 没有把 umask 设为 002 —— code_run 建的目录/文件会组不可写"
    )


def test_umask_covers_both_daemon_and_oneshot():
    """两条执行路径都必须覆盖，不能只设一条。

    daemon 走 `_child_run`（内嵌脚本里的 `os.umask(0o002)`）；
    一次性模式有两支（python / sh），**两支都要设** —— 它们都是
    `_build_argv` 拼出来的真实执行命令。

    ## 三次踩坑，都记在这里

    1. 第一版用「数 umask 出现次数 >= 4」→ 注释里也反复出现这个词，
       去掉一处仍然绿。**计数器数的是"文本提过几次"，不是"代码设过几次"**。
    2. 第二版只查 `_build_argv` 里有 `"umask 002;"` → 但 sh 分支那行
       也满足，去掉 python 分支仍然绿。**一个锚点覆盖两支代码，等于没区分**。
    3. 所以现在**逐支断言**：python 分支的 argv 与 sh 分支的 argv 各查一次。
    """
    src = BWRAP.read_text(encoding="utf-8")

    child = _function_body(src, "_child_run")
    assert re.search(r"os\.umask\(\s*0o0*2\s*\)", child), (
        "_child_run（daemon 模式）里没有 os.umask(0o002)"
    )

    build = _function_body(src, "_build_argv")
    # python 分支：`sh -c "umask 002; exec python3 -c \"$0\""`
    assert re.search(r'umask\s+002;\s*exec\s+python3', build), (
        "_build_argv 的 python 分支没有 umask 002 前缀"
    )
    # sh 分支：`sh -c f"umask 002; {code}"`
    assert re.search(r'umask\s+002;\s*\{code\}', build), (
        "_build_argv 的 sh 分支没有 umask 002 前缀"
    )


def _function_body(src: str, name: str) -> str:
    """截出某个函数/方法的源码段（到下一个同缩进 `def` 为止）。"""
    lines = src.splitlines()
    start = None
    indent = ""
    for i, line in enumerate(lines):
        if re.match(rf"^(\s*)def {re.escape(name)}\(", line):
            start = i
            indent = re.match(r"^(\s*)", line).group(1)
            break
    assert start is not None, f"找不到函数 {name}"
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.strip() and not line.startswith(indent + " ") and \
                re.match(rf"^{re.escape(indent)}(def |class |@)", line):
            break
        out.append(line)
    return "\n".join(out)


def test_oneshot_python_still_execs():
    """一次性模式包了一层 sh，必须 `exec` —— 否则 sh 会留下当父进程。

    不用 `exec` 的后果：`sh` 变成 python 的父进程，信号转发、
    exit code、以及超时时 kill 的进程树都多一层，语义与直接跑不一致。
    """
    src = BWRAP.read_text(encoding="utf-8")
    assert "exec python3" in src, "一次性模式没有 exec python3，会多留一个 sh 父进程"
