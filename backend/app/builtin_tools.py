"""Built-in agent tools (self-built, no langchain): read/write/patch/run code/load skill.

Every tool is a plain ``Tool`` (name + JSON schema + async callable) so it registers
into the same ``ToolRegistry`` as MCP tools. File access is confined by ``workspace``:
  - read:  workspace + visible skill dirs
  - write: workspace only
  - run:   cwd fixed to workspace, timeout, output truncation, stdin closed

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

from .skill_store import load_skill_content
from .tools import Tool
from .workspace import (
    PathSecurityError,
    safe_read_path,
    safe_workspace_path,
    user_workspace,
)

DEFAULT_READ_LINES = 200
MAX_OUTPUT_CHARS = 20000
DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 600


def _err(msg: str) -> str:
    return f"[工具执行错误] {msg}"


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit]
    return head + f"\n\n[输出被截断，共 {len(text)} 字符，仅显示前 {limit}]"


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
        header = f"[FILE] {target} | 共 {total} 行，显示 {start}-{end}\n"
        return header + body
    return "\n".join(chunk)


# ---------------- file_write ----------------
def _write_impl(user_id: str, path: str, content: str, mode: str = "overwrite") -> str:
    try:
        target = safe_workspace_path(user_id, path)
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
    return f"已写入 {target}（{len(new_text)} 字符，mode={mode}）"


# ---------------- file_patch ----------------
def _patch_impl(user_id: str, path: str, old_content: str, new_content: str) -> str:
    try:
        target = safe_workspace_path(user_id, path)
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
    occurrences = full.count(old_content)
    if occurrences == 0:
        return _err("未找到匹配的旧文本块。请先用 file_read 确认当前内容，再分小段 patch。")
    if occurrences > 1:
        return _err(f"找到 {occurrences} 处匹配，无法确定唯一位置。请提供更长、更具体的上下文。")
    updated = full.replace(old_content, new_content, 1)
    with open(target, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(updated)
    return f"已局部修改 {target}（替换 1 处）"


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
def _run_code_sync(code: str, code_type: str, cwd: Path, timeout: int) -> dict:
    tmp_path = None
    try:
        if code_type in ("python", "py"):
            fd, tmp_path = tempfile.mkstemp(suffix=".ai.py", prefix="inner_")
            os.close(fd)
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(code)
            cmd = [sys.executable, "-X", "utf8", "-u", tmp_path]
        elif code_type in ("powershell", "ps1", "pwsh", "bash", "sh", "shell"):
            if os.name == "nt":
                cmd = ["powershell", "-NoProfile", "-NonInteractive", "-Command", code]
            else:
                cmd = ["bash", "-c", code]
        else:
            return {"status": "error", "msg": f"不支持的类型: {code_type}"}

        creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
        proc = subprocess.run(
            cmd, cwd=str(cwd), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            timeout=timeout, creationflags=creationflags,
        )
        out = proc.stdout.decode("utf-8", errors="replace")
        return {"status": "success" if proc.returncode == 0 else "error",
                "exit_code": proc.returncode, "output": out}
    except subprocess.TimeoutExpired:
        return {"status": "error", "msg": f"执行超时（{timeout}s），已终止"}
    except Exception as e:  # noqa: BLE001
        return {"status": "error", "msg": str(e)}
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


async def _run_code_impl(user_id: str, code: str, code_type: str = "python",
                         timeout: int = DEFAULT_TIMEOUT) -> str:
    cwd = user_workspace(user_id)
    timeout = max(1, min(int(timeout or DEFAULT_TIMEOUT), MAX_TIMEOUT))
    result = await asyncio.to_thread(_run_code_sync, code, (code_type or "python").lower(), cwd, timeout)
    if result.get("status") == "error" and "output" not in result:
        return _err(result.get("msg", "执行失败"))
    output = result.get("output", "")
    header = f"[code_run {code_type}] exit={result.get('exit_code')} cwd={cwd}\n"
    return _truncate(header + output)


# ---------------- load_skill ----------------
def _load_skill_impl(user_id: str, skill: str, args: str = "") -> str:
    try:
        info = load_skill_content(user_id, skill, args or "")
    except KeyError as e:
        return _err(str(e))
    except Exception as e:  # noqa: BLE001
        return _err(f"加载 Skill 失败: {e}")
    return info["content"]


def build_builtin_tools(user_id: str) -> list[Tool]:
    """Create the built-in tool set bound to one user (closures capture user_id)."""

    async def file_read(args: dict) -> str:
        return _read_impl(
            user_id,
            str(args.get("path", "")),
            start=int(args.get("start", 1) or 1),
            count=int(args.get("count", DEFAULT_READ_LINES) or DEFAULT_READ_LINES),
            keyword=(args.get("keyword") or None),
            show_linenos=bool(args.get("show_linenos", True)),
        )

    async def file_write(args: dict) -> str:
        return _write_impl(
            user_id,
            str(args.get("path", "")),
            args.get("content", ""),
            str(args.get("mode", "overwrite") or "overwrite"),
        )

    async def file_patch(args: dict) -> str:
        return _patch_impl(
            user_id,
            str(args.get("path", "")),
            str(args.get("old_content", "")),
            str(args.get("new_content", "")),
        )

    async def list_files(args: dict) -> str:
        return _list_impl(
            user_id,
            str(args.get("path", "") or ""),
            max_entries=int(args.get("max_entries", 200) or 200),
            depth=int(args.get("depth", 2) or 2),
        )

    async def code_run(args: dict) -> str:
        return await _run_code_impl(
            user_id,
            str(args.get("code", "")),
            str(args.get("type", "python") or "python"),
            int(args.get("timeout", DEFAULT_TIMEOUT) or DEFAULT_TIMEOUT),
        )

    async def load_skill(args: dict) -> str:
        return _load_skill_impl(
            user_id,
            str(args.get("skill", "") or args.get("name", "")),
            str(args.get("args", "") or ""),
        )

    return _build_tool_list(file_read, file_write, file_patch, list_files, code_run, load_skill)


def _build_tool_list(file_read, file_write, file_patch, list_files, code_run, load_skill) -> list[Tool]:
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
            "file_write",
            "写入文件（workspace 内）。mode=overwrite(默认) | append | prepend；自动创建父目录。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "workspace 内路径"},
                    "content": {"type": "string", "description": "写入内容"},
                    "mode": {"type": "string", "enum": ["overwrite", "append", "prepend"]},
                },
                "required": ["path", "content"],
            },
            file_write,
        ),
        Tool(
            "file_patch",
            "在文件中把唯一的 old_content 替换为 new_content（必须唯一匹配；0 或多处都报错）。",
            {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old_content": {"type": "string", "description": "要被替换的原文（必须全文唯一）"},
                    "new_content": {"type": "string", "description": "替换后的新内容"},
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
            "在 workspace 内执行代码并返回输出。type=python(默认, 跑脚本) | powershell(单条命令)。"
            "有超时(默认 60s)，输出会截断。适合计算与验证，不要做交互式等待。",
            {
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "要执行的代码/命令"},
                    "type": {"type": "string", "enum": ["python", "powershell"], "description": "默认 python"},
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
