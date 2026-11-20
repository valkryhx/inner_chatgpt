"""MCP 工具数的口径必须一致：`/me/mcp-effective` 与 `/me/mcp-servers/probe`。

## 为什么（2026-11-20 真机报告）

用户看到设置页「共 46 个可用工具」，点一下「重新检测」变成「共 35 个」。
同一个页面、同一份配置，数字跳了 11。

实测确认（hx 用户）：

| 端点 | 触发方式 | 返回 | 组成 |
| --- | --- | --- | --- |
| `GET /me/mcp-effective` | 打开设置页 | **46** | 11 内置 + 35 MCP |
| `POST /me/mcp-servers/probe` | 点「重新检测」 | **35** | 只有 35 MCP |

根因：两个端点各自拼 `tools` 列表，口径不同 ——
`mcp_effective` 用 `ur.registry.tools`（内置 + MCP 合并后的注册表），
`mcp_probe` 用 `ts.tools`（纯 MCP toolset）。

前端 `SettingsModal.tsx` 两个地方都渲染 `effective.length`，
于是同一个标签在两种触发路径下显示不同数字。

## 判据设计

不能只断言「两边相等」——那在两边都错（都只给 MCP）时也成立。
必须同时断言**两边都等于「内置 + MCP」的完整工具集**，
因为那才是模型实际拥有的工具面，也是「可用工具」这个词的正确含义。
"""
from __future__ import annotations

import ast
import pathlib
import re

ME_PY = pathlib.Path("backend/app/routers/me.py")
FE = pathlib.Path("frontend/src/components/SettingsModal.tsx")


def _func_src(name: str) -> str:
    """取出 me.py 里某个 `@router...def <name>` 的完整函数体源码。

    用 AST 定位而不是文本切片：文本切片会被注释里的同名串骗到
    （本仓库有过这类反面教材）。
    """
    src = ME_PY.read_text(encoding="utf-8")
    tree = ast.parse(src)
    lines = src.splitlines()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            start = min([d.lineno for d in node.decorator_list] + [node.lineno]) - 1
            end = node.end_lineno
            return "\n".join(lines[start:end])
    raise AssertionError(f"me.py 里找不到函数 {name}")


def test_probe_includes_builtin_tools():
    """★ `mcp_probe` 必须返回「内置 + MCP」，不能只有 MCP。

    这是 46→35 跳变的直接原因。
    """
    body = _func_src("mcp_probe")
    assert "build_builtin_tools" in body, (
        "mcp_probe 没有并入内置工具 —— 它只返回 MCP toolset 的 35 个，"
        "而 mcp-effective 返回 46 个，同页两个数字会跳。\n"
        f"当前实现：\n{body}")


def test_effective_includes_builtin_tools():
    """`mcp_effective` 已经在用 registry（内置 + MCP），锁住别退回去。"""
    body = _func_src("mcp_effective")
    assert "registry.tools" in body, (
        "mcp_effective 不再用 registry.tools（内置 + MCP），口径会与模型实际工具面脱节")


def test_probe_result_shape_has_separate_counts():
    """两个端点都必须能分别报出「内置数」与「MCP 数」。

    ## 为什么需要

    只给一个总数，用户仍然无法回答「46 里有几个是 MCP 的」——
    而设置页的分区本来就按 MCP 展示。当数字变化时，也没法判断
    「是 MCP 掉了一个」还是「内置工具变了」。
    """
    probe = _func_src("mcp_probe")
    eff = _func_src("mcp_effective")
    for body, name in ((probe, "mcp_probe"), (eff, "mcp_effective")):
        assert "builtin_count" in body, (
            f"{name} 没报出内置工具数，用户无法解释工具总数变了多少")
        assert "mcp_count" in body, (
            f"{name} 没报出 MCP 工具数，与设置页的分区展示对不上")


def test_frontend_renders_split_counts():
    """前端必须把「MCP N 个 · 内置 M 个」分开显示，不能只给一个总数。

    ## 判据

    允许保留「共 N 个可用工具」这类**兜底**文案（老后端没有计数字段时用），
    但**主路径**必须显示拆分。所以检查：
    1. 有 `MCP ... 内置 ...` 的渲染表达式；
    2. 兜底分支必须由 `builtinCount !== null` 之类的条件守卫，
       不能是无条件的主文案。
    """
    src = FE.read_text(encoding="utf-8")

    assert "MCP ${mcpCount} 个" in src, (
        "前端没有渲染 MCP 工具数 —— 用户无法把 46 拆成「MCP 35 + 内置 11」")
    assert "内置 ${builtinCount} 个" in src, (
        "前端没有渲染内置工具数")
    assert "builtinCount !== null" in src and "mcpCount !== null" in src, (
        "拆分渲染没有 null 守卫，老后端（无计数字段）会显示成 undefined")

    # 兜底文案不能作为无条件的唯一显示路径
    m = re.search(r"\{mcpState !== 'ready'", src)
    assert m, "工具数渲染没有区分「检测中」，会显示 0"

    # 后端两个端点都必须报出两个计数（前端才有数可用）
    for fn in ("mcp_probe", "mcp_effective"):
        body = _func_src(fn)
        assert "builtin_count" in body and "mcp_count" in body, (
            f"{fn} 未报出拆分计数，前端只能拿到总数")


# --------------------------------------------------------------------------
# ★ 行为测试（源码字符串判据不够，见下）
# --------------------------------------------------------------------------

def test_probe_behaviourally_returns_builtin_plus_mcp(monkeypatch):
    """真正调用 `mcp_probe`，验证它返回的 tools 含内置工具。

    ## 为什么必须行为测试而不是查源码字符串

    首版用 `assert "build_builtin_tools" in body`。反向确认把 **调用** 删掉后
    测试仍绿 —— 因为那个字符串还留在内层 helper `_builtin_names` 的
    **定义**里。字符串存在 ≠ 真的用了。

    这正是 AGENTS.md 说的「判据必须能区分两种状态」：源码里出现某个名字，
    在「用了」和「只定义没用」两种状态下读数相同 ⇒ 不是判据。
    """
    import asyncio
    from app.routers import me as me_mod

    class FakeToolset:
        tools = [type("T", (), {"name": "sqlbot_t1"})(),
                 type("T", (), {"name": "falcomm_t1"})()]
        manifest = [{"name": "sqlbotmcp", "ok": True, "tool_count": 2}]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return None

    async def fake_build_user_toolset(_uid):
        return FakeToolset()

    builtin = [type("T", (), {"name": n})() for n in
               ("file_read", "file_write", "code_run")]

    monkeypatch.setattr("app.agent.build_user_toolset", fake_build_user_toolset)
    monkeypatch.setattr("app.builtin_tools.build_builtin_tools",
                        lambda _uid, **_kw: builtin)

    res = asyncio.run(me_mod.mcp_probe(user={"id": "u_test", "role": "user"}))

    assert res["ok"] is True
    assert res["builtin_count"] == 3, f"builtin_count 不对：{res}"
    assert res["mcp_count"] == 2, f"mcp_count 不对：{res}"
    assert len(res["tools"]) == 5, (
        f"tools 只有 {len(res['tools'])} 个（{res['tools']}）—— "
        "没有并入内置工具，这正是 46→35 跳变的原因")
    for n in ("file_read", "code_run", "sqlbot_t1"):
        assert n in res["tools"], f"{n} 不在返回里：{res['tools']}"


def test_probe_failure_still_reports_builtin(monkeypatch):
    """MCP 全挂时，probe 仍应返回内置工具数（不能报 0 让用户以为什么都没有）。"""
    import asyncio
    from app.routers import me as me_mod

    async def boom(_uid):
        raise RuntimeError("mcp down")

    builtin = [type("T", (), {"name": "file_read"})()]
    monkeypatch.setattr("app.agent.build_user_toolset", boom)
    monkeypatch.setattr("app.builtin_tools.build_builtin_tools",
                        lambda _uid, **_kw: builtin)

    res = asyncio.run(me_mod.mcp_probe(user={"id": "u_test", "role": "user"}))
    assert res["ok"] is False
    assert res["builtin_count"] == 1, f"失败路径丢了内置工具数：{res}"
    assert res["mcp_count"] == 0
    assert "mcp down" in res["error"]
