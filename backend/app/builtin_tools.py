"""Built-in agent tools (self-built, no langchain): read/write/patch/run code/load skill.

Every tool is a plain ``Tool`` (name + JSON schema + async callable) so it registers
into the same ``ToolRegistry`` as MCP tools. File access is confined by ``workspace``:
  - read:  workspace + visible skill dirs
  - write: workspace only
  - run:   cwd + Python write audit confined to workspace, timeout, output truncation

Design reference: GenericAgent genericagent/core/handler.py
(file_read / file_patch / code_run / do_load_skill). Individually reimplemented.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .run_guard import WORKSPACE_PYTHON_GUARD, protected_code_reason, workspace_env
from .skill_store import load_skill_content
from .file_search import file_glob as _file_glob, file_grep as _file_grep
from . import edit_match
from .tools import Tool
from .workspace import (
    PathSecurityError,
    display_path,
    safe_read_path,
    safe_workspace_path,
    user_workspace,
    write_root_for,
)

# 单次 file_read 默认读多少行。**对齐 CC 的 2000**（319 `m0274:604` `$tn = 2000`，
# 提示词 `claude-code-fable-5.2.md:2331` "Reads up to 2000 lines by default"）。
#
# 从 200 提到 2000（2026-11-20）的理由是**轮次成本**，不是"抄得像"：
# 一个 40 KB 的 markdown 报告约 1000-1500 行，旧默认下模型要翻 5-8 次才能读完，
# 每次都是一轮 LLM 调用。实测 Agent Team 的子代理把大量轮次花在翻页上。
#
# 上限 2000 仍然夹住（见 `_read_impl` 里的 `min(..., 2000)`）：一次读太多会挤爆
# 上下文预算，而预算被挤爆的代价（压缩、丢中间内容）远大于多翻一页。
DEFAULT_READ_LINES = 2000
MAX_OUTPUT_CHARS = 20000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def _err(msg: str) -> str:
    return f"[工具执行错误] {msg}"


def _find_posix_bash() -> str | None:
    """在 Windows 上找一个能跑 POSIX 语义的 bash，找不到返回 None。

    为什么需要：`code_run(type="sh")` 的语义是 **POSIX shell**，
    目的是让同一段命令在 Windows 宿主与 Linux 沙箱容器里行为一致。
    Windows 自带的 `powershell` 语义完全不同（`ls` 是 alias、`cat` 是
    `Get-Content`），拿它冒充 sh 会让模型写出「本地能跑、沙箱里挂掉」的代码。

    查找顺序（不依赖 PATH 里的 `bash`，本机 `bash` 可能命中 WSL 存根）：
    1. ``HLZ_POSIX_BASH`` 环境变量（显式覆盖）
    2. Git for Windows 的常规安装位置
    3. PATH 里的 ``bash`` —— 但要排除 System32 的 WSL 存根
    """
    import os
    import shutil

    explicit = (os.environ.get("HLZ_POSIX_BASH") or "").strip()
    if explicit and Path(explicit).is_file():
        return explicit

    candidates = [
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files\Git\usr\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Programs\Git\bin\bash.exe"),
        os.path.expandvars(r"%USERPROFILE%\scoop\apps\git\current\bin\bash.exe"),
    ]
    for c in candidates:
        try:
            if c and Path(c).is_file():
                return c
        except OSError:
            continue

    found = shutil.which("bash")
    if found and "system32" not in found.lower():
        return found
    return None


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit]
    return head + f"\n\n[输出被截断，共 {len(text)} 字符，仅显示前 {limit}]"


def format_code_run_header(code_type: str, code: str, exit_code: int, cwd) -> str:
    """code_run 的第一行：把非零退出码翻译成模型能直接用的语义。"""
    from .exit_codes import exit_code_note

    note = exit_code_note(code, exit_code) if exit_code else ""
    suffix = f" ({note})" if note else ""
    return f"[code_run {code_type}] exit={exit_code}{suffix} cwd={cwd}" + chr(10)


def _sandbox_bypass_allowed(tool: str) -> bool:
    """沙箱分流抛异常时，能不能回落进程内执行？

    ## 这是一个安全判断，不是容错判断

    原来三处沙箱分流都写成：

        except Exception:   # noqa: BLE001 - 沙箱故障不能打断工具本身
            logger.warning("...回落进程内")
        return _impl(...)   # ← 无隔离执行

    初衷是「别让沙箱的 bug 弄挂工具」，但它把 **strict 的安全承诺**一起吞了：
    `manager._handle_unavailable` 在 strict 下刻意 `raise SandboxUnavailable`
    （意思就是「不许跑」），这个裸 except 把异常接住后照跑不误。

    实测（2026-11-19）：`HLZ_SANDBOX_MODE=strict` + 沙箱不可用，代码仍然在
    app 进程里执行成功、文件真的落盘了。用户看不到任何提示 ——
    **比 auto 更危险**，因为 auto 至少会在结果首行标「本次未隔离」。

    ## 规则

    - `strict`：**任何**沙箱分流异常都不回落。用户的明确选择是
      「宁可失败也不能在无隔离下跑」，容错不能覆盖这个选择。
    - `auto`：回落进程内（这是它的设计），但调用方必须把「未隔离」标记
      放在结果首行，不能静默。
    - `off`：压根不会走到分流分支。

    Returns:
        True 表示允许回落进程内；False 表示必须拒绝执行。
    """
    from .sandbox.config import get_config

    mode = get_config().mode
    if mode == "strict":
        import logging

        logging.getLogger(__name__).warning(
            "沙箱分流失败且 mode=strict：不回落进程内（tool=%s）。"
            "这是安全语义，不是 bug —— 要允许降级请改用 auto。",
            tool,
            exc_info=True,
        )
        return False
    return True


def _with_budget(user_id: str, tool_name: str, text: str) -> str:
    """统一结果预算：超限落盘到 workspace/.hlaudz/tool_results 并给回可读路径。"""
    from .tool_budget import apply_budget, budget_dir_for

    try:
        where = budget_dir_for(user_workspace(user_id))
    except Exception:  # noqa: BLE001 - 预算层不能因为目录问题让工具失败
        where = None
    return apply_budget(tool_name, text, persist_dir=where)


# ---------------- file_read ----------------
def _read_impl(user_id: str, path: str, start: int = 1, count: int = DEFAULT_READ_LINES,
               keyword: str | None = None, show_linenos: bool = True) -> str:
    try:
        target = safe_read_path(user_id, path)
    except PathSecurityError as e:
        return _err(str(e))
    if not target.is_file():
        return _err(f"文件不存在: {path}")
    try:
        text = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _err(f"读取失败: {e}")
    all_lines = text.splitlines()
    total = len(all_lines)
    start = max(1, int(start or 1))
    count = max(1, min(int(count or DEFAULT_READ_LINES), 2000))
    if keyword:
        kw = keyword.lower()
        idx = next((i for i in range(start - 1, total) if kw in all_lines[i].lower()), None)
        if idx is None:
            return f"未找到关键字 {keyword!r}（从第 {start} 行起）。文件共 {total} 行。"
        start = idx + 1
    end = min(total, start - 1 + count)
    chunk = all_lines[start - 1:end]
    if show_linenos:
        body = "\n".join(f"{start + i}|{line}" for i, line in enumerate(chunk))
        header = f"[FILE] {display_path(user_id, target)} | 共 {total} 行，显示 {start}-{end}\n"
        return header + body + _truncation_banner(
            user_id, target, start=start, end=end, total=total)
    tail = "\n".join(chunk)
    # show_linenos=False 时仍要提示截断 —— 否则模型以为自己拿到了全文。
    # 这是「不显示行号」和「不告知未读完」两件事，不能混。
    return tail + _truncation_banner(
        user_id, target, start=start, end=end, total=total)


def _truncation_banner(user_id: str, target, *, start: int, end: int,
                       total: int) -> str:
    r"""截断时追加的横幅：**明确告诉模型下一步怎么读**。

    ## 对齐 CC（319 `m0591:148116`）

    CC 的原文模板（按能否按行分页分两档）：

        [Truncated: PARTIAL view — <file>: showing lines 1-N of M total
        (T tokens, cap G). Call <Read> with offset=N+1 limit=N for the next page,
        or <Grep> to find a specific section. Do NOT answer from this page alone
        if the answer may be further in the file.]

    三件事一起做：① 声明这是**部分视图**；② 给出**可直接照抄的参数**
    （`offset=N+1 limit=N`）；③ 警告别只看这一页就下结论。

    ## 为什么必须写出具体参数

    只写「显示 1-200，共 2500 行」，模型得自己推「下一步 start=201」。
    实测（Agent Team 子代理）它会重读同一段、或者干脆放弃阅读直接瞎写。
    CC 把下一步写死，模型不需要推理就能继续 —— 这是**消除一类失败模式**，
    不是提供便利。

    ## 为什么必须有第 ③ 条

    没有它时，模型容易把「我看到的这部分」当成「全部」，据此下结论。
    CC 用 `Do NOT answer from this page alone` 明说。这里保留等效的中文表述。

    Args:
        start/end: 本次实际返回的 1-based 行区间（闭区间）。
        total: 文件总行数。

    Returns:
        空串（没截断）或 `\n\n` 开头的横幅。
    """
    if end >= total:
        return ""
    nxt = end + 1
    shown = display_path(user_id, target)
    remain = total - end
    return (
        f"\n\n[Truncated: 部分视图 — {shown}: 显示 {start}-{end}，共 {total} 行，"
        f"还有 {remain} 行未显示。\n"
        f"下一页：file_read path={shown!r} start={nxt} count={end - start + 1}\n"
        f"找特定内容：file_read path={shown!r} keyword=<关键字>\n"
        f"不要在只看这一页的情况下下结论 —— 答案可能在未显示的部分。]"
    )


# ---------------- file_write ----------------
def _write_impl(user_id: str, path: str, content: str, mode: str = "overwrite",
                *, root: str = "") -> str:
    try:
        target = safe_workspace_path(user_id, path, root=root)
    except PathSecurityError as e:
        return _err(str(e))
    mode = (mode or "overwrite").lower()
    if mode not in ("overwrite", "append", "prepend"):
        return _err(f"不支持的 mode: {mode}（overwrite/append/prepend）")
    target.parent.mkdir(parents=True, exist_ok=True)
    content = content if isinstance(content, str) else str(content)
    existing = ""
    if mode != "overwrite" and target.exists():
        try:
            existing = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            existing = ""
    if mode == "append":
        new_text = existing + content
    elif mode == "prepend":
        new_text = content + existing
    else:
        new_text = content
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(new_text)
    # 用相对路径：绝对路径会泄露宿主 OS 与目录结构（见 workspace.display_path）
    return f"已写入 {display_path(user_id, target)}（{len(new_text)} 字符，mode={mode}）"


# ---------------- file_patch ----------------
def _patch_impl(user_id: str, path: str, old_content: str, new_content: str,
                *, root: str = "", replace_all: bool = False) -> str:
    try:
        target = safe_workspace_path(user_id, path, root=root)
    except PathSecurityError as e:
        return _err(str(e))
    if not target.is_file():
        return _err(f"文件不存在: {path}")
    if not old_content:
        return _err("old_content 不能为空")
    try:
        full = target.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return _err(f"读取失败: {e}")
    if old_content == new_content:
        # 对齐 CC：`No changes to make: old_string and new_string are exactly the same.`
        # 这是模型常见的无效调用（以为改了其实没改），直接告诉它不用重试。
        return _err("old_content 与 new_content 完全相同，没有任何改动，无需重试。")

    # ★ 降级匹配（对齐 CC319 的 Edit，见 app/edit_match.py）。
    #   精确匹配失败时依次尝试：引号归一化 → \uXXXX 还原 → 转义形态匹配。
    #   返回的是**文件里真实存在的文本**，所以替换用的是原文，
    #   模型抄错的引号/转义不会被写进文件。
    actual = edit_match.find_actual(old_content, full)
    if actual is None:
        return _err(edit_match.describe_failure(old_content, full))

    occurrences = full.count(actual)
    if occurrences > 1 and not replace_all:
        return _err(
            f"找到 {occurrences} 处匹配，但 replace_all=false，无法确定替换哪一处。"
            f"要全部替换请传 replace_all=true；只替换一处请提供更长、"
            f"能唯一定位的 old_content（当前匹配到的原文：{actual[:80]!r}）。")

    if replace_all:
        updated = full.replace(actual, new_content)
        rewritten = True
    else:
        updated = full.replace(actual, new_content, 1)
        rewritten = actual != old_content

    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    note = ""
    if rewritten and not replace_all:
        # 降级匹配生效时明确告知，便于模型理解「为什么我抄的不一样也成功了」
        note = "（old_content 与原文不完全一致，已按文件中的实际文本匹配替换）"
    count = occurrences if replace_all else 1
    return f"已局部修改 {display_path(user_id, target)}（替换 {count} 处）{note}"


# ---------------- list_files ----------------
def _list_impl(user_id: str, path: str = "", max_entries: int = 200, depth: int = 2) -> str:
    try:
        root = safe_read_path(user_id, path) if (path or "").strip() else user_workspace(user_id)
    except PathSecurityError as e:
        return _err(str(e))
    if not root.exists():
        return _err(f"路径不存在: {path}")
    if root.is_file():
        return str(root)
    max_entries = max(1, min(int(max_entries or 200), 1000))
    depth = max(0, min(int(depth or 2), 5))
    out: list[str] = []
    base = root
    for dirpath, dirnames, filenames in os.walk(base):
        rel = Path(dirpath).relative_to(base)
        cur_depth = 0 if str(rel) == "." else len(rel.parts)
        if cur_depth >= depth:
            dirnames[:] = []
        for d in sorted(dirnames):
            out.append(f"[D] {(rel / d).as_posix()}")
        for f in sorted(filenames):
            out.append(f"[F] {(rel / f).as_posix()}")
        if len(out) >= max_entries:
            out = out[:max_entries]
            out.append(f"...(已截断，最多 {max_entries} 项)")
            break
    return f"[{base}]\n" + "\n".join(out) if out else f"[{base}] (空)"


# ---------------- code_run ----------------
async def _run_code_impl(
    user_id: str,
    code: str,
    code_type: str = "python",
    timeout: int = DEFAULT_TIMEOUT,
    *,
    root: str = "",
    session_id: str = "",
    generation_id: str = "",
    agent_id: str = "root",
) -> str:
    from .process_registry import (
        ProcessLease, register, spawn_process, unregister, _kill_tree,
    )
    from .quota import QuotaExceeded, get_quota

    cwd = write_root_for(user_id, root)
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    code_type = (code_type or "python").lower()
    blocked = protected_code_reason(code)
    if blocked:
        return _err(blocked)

    tmp_path = None
    proc = None
    lease = ProcessLease(user_id, session_id, generation_id, agent_id)
    try:
        if code_type in ("python", "py"):
            fd, tmp_path = tempfile.mkstemp(suffix=".ai.py", prefix="inner_")
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(code)
            cmd = [sys.executable, "-X", "utf8", "-u", "-c",
                   WORKSPACE_PYTHON_GUARD, tmp_path, str(cwd)]
        elif code_type in ("powershell", "ps1", "pwsh"):
            # 显式要求 powershell：只有 Windows 有，Linux 直接报可操作的错
            if os.name == "nt":
                cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
            else:
                return _err(
                    "Linux 环境没有 powershell。请改用 type=sh（POSIX shell），"
                    "或用 type=python 通过 subprocess 调用。")
        elif code_type in ("sh", "bash", "shell"):
            # POSIX shell。Windows 宿主机上用 Git Bash 的 bash，保证 `ls`/`cat`
            # 这类命令的语义与沙箱容器内一致（而不是 powershell 的 alias）。
            if os.name == "nt":
                bash = _find_posix_bash()
                if bash is None:
                    return _err(
                        "本机找不到 bash（Git Bash）。请改用 type=python，"
                        "或安装 Git for Windows 后重试。")
                cmd = [bash, "-c", code]
            else:
                cmd = ["bash", "-c", code]
        else:
            return _err(f"不支持的类型: {code_type}")

        async with get_quota().code_run(user_id, timeout_s=2.0):
            await register(lease)
            creationflags = 0x08000000 if os.name == "nt" else 0
            kwargs = {}
            if os.name == "nt":
                kwargs["creationflags"] = creationflags
            else:
                kwargs["start_new_session"] = True
            proc = await spawn_process(
                cmd,
                cwd=str(cwd),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                env=workspace_env(cwd),
                **kwargs,
            )
            lease.proc = proc
            try:
                stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except (asyncio.TimeoutError, TimeoutError):
                await _kill_tree(proc)
                return _err(f"执行超时（{timeout}s），已终止")
            except asyncio.CancelledError:
                await _kill_tree(proc)
                raise
            out = (stdout or b"").decode("utf-8", errors="replace")
            if lease.stopped:
                return _err("已停止：执行进程已被终止")
            header = format_code_run_header(code_type, code, proc.returncode, cwd)
            return header + out
    except QuotaExceeded as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        if proc is not None and proc.returncode is None:
            await _kill_tree(proc)
        detail = str(e).strip()
        return _err(f"{type(e).__name__}: {detail}" if detail else type(e).__name__)
    finally:
        await unregister(lease)
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ---------------- load_skill ----------------
def _load_skill_impl(user_id: str, skill: str, args: str = "") -> str:
    try:
        info = load_skill_content(user_id, skill, args or "")
    except KeyError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"加载 Skill 失败: {e}")
    return info["content"]


def build_builtin_tools(
    user_id: str,
    *,
    write_root: str | None = None,
    session_id: str = "",
    generation_id: str = "",
    team_id: str = "",
    agent_id: str = "root",
    include_memory: bool = True,
) -> list[Tool]:
    """Create the built-in tool set bound to one user (closures capture user_id).

    ``write_root`` 可选：限定「写」类工具（file_write / file_patch / code_run）的根目录。
    subagent 用它把自己的产物隔离到 ``.subagents/<session>/<agent>`` 下，
    避免多个子代理并发写同一个 workspace 互相覆盖（设计文档 §7.2）。

    ``include_memory``：是否装配 ``memory_list/read/write``。默认 True（root agent），
    **子代理必须显式传 False** —— 记忆工具只给 root，子代理要记忆应由 root 给它上下文
    （记忆方案 §5.2）。用参数而不是事后过滤，是为了让「子代理 registry 里根本没有它们」
    这条边界在装配处就成立。
    """
    from .memory_store import memory_enabled

    root = write_root or ""

    async def file_read(args: dict) -> str:
        return _with_budget(user_id, "file_read", _read_impl(
            user_id,
            str(args.get("path", "")),
            start=int(args.get("start", 1) or 1),
            count=int(args.get("count", DEFAULT_READ_LINES) or DEFAULT_READ_LINES),
            keyword=(args.get("keyword") or None),
            show_linenos=bool(args.get("show_linenos", True)),
        ))

    async def file_write(args: dict) -> str:
        text = str(args.get("content", ""))
        mode = str(args.get("mode", "overwrite") or "overwrite")
        # 沙箱分流（设计文档 §7.2）：只改执行位置，校验仍在宿主进程做
        try:
            from .sandbox import policy as sbx_policy
            if sbx_policy.should_use_sandbox("file_write", user_id):
                from .sandbox.manager import get_manager
                handled, note = await get_manager().write_file(
                    user_id, str(args.get("path", "")), text,
                    mode=mode, root=root,
                )
                if handled:
                    return note
                # 降级（auto）：把「未隔离」标记拼在进程内结果前面，绝不静默
                return note + "\n" + _write_impl(
                    user_id, str(args.get("path", "")), text, mode,
                    root=root,
                )
        except Exception as exc:  # noqa: BLE001
            # strict 下不回落进程内（否则等于没有沙箱）。返回失败结果，不向上抛
            # —— 抛异常会打断 agent 循环。
            if not _sandbox_bypass_allowed("file_write"):
                return f"[sandbox] 沙箱不可用，已按 strict 拒绝写入：{exc}"
            import logging
            logging.getLogger(__name__).warning("file_write 沙箱分流失败，回落进程内", exc_info=True)
        return _write_impl(
            user_id,
            str(args.get("path", "")),
            text,
            mode,
            root=root,
        )

    async def file_patch(args: dict) -> str:
        old_c = str(args.get("old_content", ""))
        new_c = str(args.get("new_content", ""))
        replace_all = bool(args.get("replace_all", False))
        try:
            from .sandbox import policy as sbx_policy
            if sbx_policy.should_use_sandbox("file_patch", user_id):
                from .sandbox.manager import get_manager
                handled, note = await get_manager().patch_file(
                    user_id, str(args.get("path", "")), old_c, new_c,
                    root=root, replace_all=replace_all,
                )
                if handled:
                    return note
                return note + "\n" + _patch_impl(
                    user_id, str(args.get("path", "")), old_c, new_c,
                    root=root, replace_all=replace_all,
                )
        except Exception as exc:  # noqa: BLE001
            # strict 下不回落进程内（否则等于没有沙箱）。返回失败结果，不向上抛
            # —— 抛异常会打断 agent 循环。
            if not _sandbox_bypass_allowed("file_patch"):
                return f"[sandbox] 沙箱不可用，已按 strict 拒绝修改：{exc}"
            import logging
            logging.getLogger(__name__).warning("file_patch 沙箱分流失败，回落进程内", exc_info=True)
        return _patch_impl(
            user_id,
            str(args.get("path", "")),
            old_c,
            new_c,
            root=root,
            replace_all=replace_all,
        )

    async def list_files(args: dict) -> str:
        return _list_impl(
            user_id,
            str(args.get("path", "") or ""),
            max_entries=int(args.get("max_entries", 200) or 200),
            depth=int(args.get("depth", 2) or 2),
        )

    async def code_run(args: dict) -> str:
        code = str(args.get("code", ""))
        ctype = str(args.get("type", "python") or "python")
        tmo = int(args.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT)
        # 沙箱分流（设计文档 §7.1）
        try:
            from .sandbox import policy as sbx_policy
            if sbx_policy.should_use_sandbox("code_run", user_id):
                from .sandbox.manager import get_manager
                handled, out, _marker = await get_manager().exec_code(
                    user_id, code, code_type=ctype, timeout=tmo,
                    root=root, session_id=session_id,
                    generation_id=generation_id, agent_id=agent_id,
                )
                if handled:
                    return _with_budget(user_id, "code_run", out)
                # 降级（auto）：标记 + 进程内真实执行
                raw = await _run_code_impl(
                    user_id, code, ctype, tmo,
                    root=root, session_id=session_id,
                    generation_id=generation_id, agent_id=agent_id,
                )
                return _with_budget(user_id, "code_run", out + "\n" + raw)
        except Exception as exc:  # noqa: BLE001
            # strict 下不回落进程内（否则等于没有沙箱）。返回失败结果，不向上抛
            # —— 抛异常会打断 agent 循环。这正是实测到的漏洞：
            # strict + 沙箱不可用时代码仍然在 app 进程里执行了。
            if not _sandbox_bypass_allowed("code_run"):
                return f"[sandbox] 沙箱不可用，已按 strict 拒绝执行：{exc}"
            import logging
            logging.getLogger(__name__).warning("code_run 沙箱分流失败，回落进程内", exc_info=True)
        raw = await _run_code_impl(
            user_id,
            code,
            ctype,
            tmo,
            root=root,
            session_id=session_id,
            generation_id=generation_id,
            agent_id=agent_id,
        )
        return _with_budget(user_id, "code_run", raw)

    async def file_glob(args: dict) -> str:
        return _with_budget(user_id, "file_glob", _file_glob(
            user_id,
            str(args.get("pattern", "")),
            str(args.get("path", "") or ""),
        ))

    async def file_grep(args: dict) -> str:
        return _with_budget(user_id, "file_grep", _file_grep(
            user_id,
            str(args.get("pattern", "")),
            str(args.get("path", "") or ""),
            str(args.get("glob", "") or ""),
            str(args.get("output_mode", "content") or "content"),
            bool(args.get("ignore_case", False)),
            int(args.get("context", 0) or 0),
            int(args.get("head_limit", 250) or 250),
        ))

    async def load_skill(args: dict) -> str:
        return _load_skill_impl(
            user_id,
            str(args.get("skill", "") or args.get("name", "")),
            str(args.get("args", "") or ""),
        )

    tools = _build_tool_list(file_read, file_write, file_patch, list_files, code_run,
                             load_skill, file_glob, file_grep)
    if include_memory and memory_enabled():
        try:
            from .memory_tools import build_memory_tools
            tools.extend(build_memory_tools(user_id))
        except Exception:  # noqa: BLE001 - 记忆装配失败不该让整个工具面消失
            import logging
            logging.getLogger(__name__).warning("记忆工具装配失败，已跳过", exc_info=True)
    return tools


def _build_tool_list(file_read, file_write, file_patch, list_files, code_run, load_skill,
                    file_glob, file_grep) -> list[Tool]:
    return [
        Tool(
            "file_read",
            "读取文本文件内容。path 可为 workspace 内相对路径或绝对路径（也允许读取可见 Skill 目录）。"
            "支持 start(从第几行,1-based) / count(默认200) / keyword(定位关键字) / show_linenos。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径"},
                    "start": {"type": "integer", "description": "起始行，1-based，默认 1"},
                    "count": {"type": "integer", "description": "读取行数，默认 200"},
                    "keyword": {"type": "string", "description": "可选：只显示包含该关键字的区域"},
                    "show_linenos": {"type": "boolean", "description": "是否显示行号，默认 true"},
                },
                "required": ["path"],
            },
            file_read,
        ),
        Tool(
            "file_glob",
            "按文件名/路径模式查找文件。找「哪些文件叫 xxx」「这个项目有哪些 yyy 类型文件」时用它，"
            "不要用 code_run 跑 find / ls。pattern 支持 **/*.py 这类写法；最多返回 100 个路径，但返回值里的 total 是完整数量，可直接用来统计文件个数。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "glob 模式，如 **/*.py"},
                    "path": {"type": "string", "description": "搜索根目录，省略=workspace 根"},
                },
                "required": ["pattern"],
            },
            file_glob,
        ),
        Tool(
            "file_grep",
            "在文件内容里搜索。找「哪处引用了 X」「有哪些 TODO」时用它，不要用 code_run 跑 grep / rg。"
            "先用 output_mode=files_with_matches 收敛范围，再用 content + context 看上下文。",
            {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "正则表达式"},
                    "path": {"type": "string", "description": "搜索根目录，省略=workspace 根"},
                    "glob": {"type": "string", "description": "文件过滤，如 *.py"},
                    "output_mode": {"type": "string",
                                    "enum": ["content", "files_with_matches", "count"],
                                    "description": "默认 content"},
                    "ignore_case": {"type": "boolean", "description": "忽略大小写"},
                    "context": {"type": "integer", "description": "前后各 N 行，最大 20"},
                    "head_limit": {"type": "integer", "description": "最多返回多少行，默认 250"},
                },
                "required": ["pattern"],
            },
            file_grep,
        ),
        Tool(
            "file_write",
            "写入文件（workspace 内）。mode=overwrite(默认) | append | prepend；自动创建父目录。"
            "\n\n路径：**相对 workspace 根**直接写，如 `a.py`、`out/data.csv`，不需要也不要用"
            "绝对路径（绝对路径在沙箱内外指向不同位置，会写丢）。"
            "\n\n覆盖已存在的文件**不需要**先 file_read（读前置已取消）。"
            "但如果要保留原有内容的一部分，先读清楚再写，避免覆盖掉自己需要的东西。"
            "\n\n**长内容请分段写**：正文超过 3000 字时，拆成多次调用——"
            "第一次 `mode=overwrite` 写开头，之后每次 `mode=append` 追加一段（每段 2000–3000 字）。"
            "原因：内容是在模型生成阶段产生的，一次性写完会让用户等几十秒只看到空白"
            "（实测写 27,000 字需要约 96 秒）；分段写则每一段几千字就能落盘，"
            "用户能更早看到进展，中途被打断也不会全部作废。"
            "注意分段只改善体感，不减少总生成时间——总时间由字数决定。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对 workspace 根，如 `a.py` 或 `out/data.csv`"},
                    "content": {"type": "string", "description": "写入内容"},
                    "mode": {"type": "string", "enum": ["overwrite", "append", "prepend"]},
                },
                "required": ["path", "content"],
            },
            file_write,
        ),
        Tool(
            "file_patch",
            "在文件中把 old_content 替换为 new_content。**只发差异，不要发整个文件**"
            "——改大文件时这是唯一可行的方式。"
            "\n\n**不需要**先 file_read（读前置已取消）。"
            "\n\n匹配是**宽容**的：除了精确匹配，还会自动兼容智能引号（“” / ‘’）"
            "与 ASCII 引号的差异、以及 \\uXXXX 转义与实际中文的差异。"
            "所以抄得不够准通常也能成功；若仍失败，说明差异在别处，请重读文件后逐字复制。"
            "\n\n默认只替换**唯一**的一处：0 处或多处都会失败。"
            "确有需要全部替换时传 `replace_all=true`。"
            "\n\n参数：`old_content` 保持最小（够唯一即可，通常 1–3 行）——"
            "包含过多上下文浪费 token。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，相对 workspace 根，如 `a.py` 或 `out/data.csv`"},
                    "old_content": {"type": "string", "description": "要被替换的原文（需要在文件内唯一；1–3 行通常够）"},
                    "new_content": {"type": "string", "description": "替换后的新内容（必须与 old_content 不同）"},
                    "replace_all": {"type": "boolean", "description": "是否替换全部匹配，默认 false（只替换唯一一处）"},
                },
                "required": ["path", "old_content", "new_content"],
            },
            file_patch,
        ),
        Tool(
            "list_files",
            "列出 workspace（或可见目录）中的文件和子目录，depth 默认 2，最多 200 项。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "目录，默认 workspace 根"},
                    "max_entries": {"type": "integer", "description": "最多条目数，默认 200"},
                    "depth": {"type": "integer", "description": "递归深度，默认 2"},
                },
            },
            list_files,
        ),
        Tool(
            "code_run",
            "在 workspace 内执行代码并返回输出。type=python(默认，跑 Python 脚本) | "
            "sh(跑 shell 命令)。有超时(默认 60s)，输出会截断。适合计算与验证，不要做交互式等待。"
            "\n\n选型：**默认用 python**。只有 shell 才能做的事（管道、系统命令、pip、"
            "进程管理）才用 sh。"
            "\n\n平台说明：type 里的 shell 写法在 Linux/沙箱容器内是 sh(POSIX)，"
            "在 Windows 宿主上是 powershell。**如果你不知道自己在哪个平台，用 sh** ——"
            "`ls`/`pwd`/`cat` 这类命令在两者上都能跑；写 PowerShell 特有语法"
            "（`dir`、`Get-ChildItem`、`$env:`）在沙箱里会失败。"
            "\n\n路径：当前目录就是你的 workspace 根，**直接用相对路径**（如 `a.py`、"
            "`out/data.csv`）。不要拼绝对路径，不要假设 C:\\ 或 /home。",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的代码/命令"},
                    "type": {
                        "type": "string",
                        "enum": ["python", "sh"],
                        "description": (
                            "python(默认) 或 sh。sh 在沙箱容器内是 POSIX shell，"
                            "在 Windows 宿主上是 powershell —— 不确定平台时用 sh。"
                        ),
                    },
                    "timeout": {"type": "integer", "description": "超时秒数，默认 60，最大 600"},
                },
                "required": ["code"],
            },
            code_run,
        ),
        Tool(
            "load_skill",
            "按名称加载一个 Skill 的完整说明（SKILL.md）。当任务与系统提示里的某个 Skill 匹配时，先调用本工具。",
            {
                "type": "object",
                "properties": {
                    "skill": {"type": "string", "description": "Skill 名称"},
                    "args": {"type": "string", "description": "可选：传给 Skill 的参数，替换 $ARGUMENTS"},
                },
                "required": ["skill"],
            },
            load_skill,
        ),
    ]
