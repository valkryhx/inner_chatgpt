"""`file_write` / `file_patch` 必须走沙箱服务（TDD RED）。

## 这一组修的是「文件工具没接 backend 分发」

实测（2026-11-20，container，`HLZ_SANDBOX_MODE=strict` + `backend=service`）：

    WARNING app.builtin_tools: 沙箱分流失败且 mode=strict：不回落进程内（tool=file_write）
    SandboxUnavailable: 未安装 docker SDK（ModuleNotFoundError）

因果链：

    manager.write_file → self.pool.acquire()（Docker 路径）
                       → app 容器里没装 docker SDK
                       → SandboxUnavailable
                       → strict 不回落 → 失败

对比 `exec_code` 有 backend 分发（`manager.py:343`），而
`write_file` / `patch_file` 都没有 —— 它们直接走 Docker。

## 后果（真机观察到的）

Agent Team 的两个子代理状态是 `stopped` 而不是 `completed`，
最后一个结果是「写入被沙箱阻塞。我先确认工作区状态，再换路径重试」。
它们每次 file_write 都被拒绝，反复重试耗尽轮次后被停掉。
对应的两个 team task 一直是 `pending`、`owner_agent_id` 为空 ——
子代理没跑到「认领任务」那一步就死了。

## 为什么让文件写进沙箱（而不是留在进程内）

「文件工具留在 app 进程内」这个说法出处有误：方案的 §3.3
「该留在 app 的」列的是 policy / 工具 schema / 预算 / 降级决策 / 总开关，
**没有文件工具**。`ServiceBackend.write_file` 里的注释把它归给 §4.2，
但 §4.2 讲的是池的设计。

走沙箱的实际收益是防御纵深：文件写操作也在沙箱的 mount namespace 里，
万一路径校验有漏洞，还有容器兜底。

## 断言口径

不看返回值文案，看**文件到底落在哪、内容对不对**。
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    from app import config as app_config
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setattr(app_config, "WORKSPACES_DIR", tmp_path)
    return tmp_path


@pytest.fixture()
def sandbox_all(monkeypatch):
    """空白名单 = 不限制 = 全部用户可进沙箱。"""
    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")
    return None


def refresh(mode: str, monkeypatch):
    """设 mode 并让单例重新求值（顺序：先 setenv 再 reload）。

    必须同时重建 manager：它的 cfg 在构造时固化。
    """
    monkeypatch.setenv("HLZ_SANDBOX_MODE", mode)
    from app.sandbox import config as sbx_config
    from app.sandbox import manager as mgr_mod

    sbx_config.get_config(reload=True)
    mgr_mod.get_manager(reload=True)


class _FakeService:
    """模拟 ServiceBackend：记录调用，真的写文件（模拟沙箱侧执行）。"""

    name = "service"

    def __init__(self, root: Path) -> None:
        self.root = root
        self.writes: list[tuple[str, str, str]] = []
        self.patches: list[tuple[str, str, str]] = []

    async def health(self):
        return True, "ok"

    async def exec_code(self, *a, **kw):
        return True, "SANDBOX", ""

    async def write_file(self, user_id, path, content, *, mode="overwrite", root=""):
        self.writes.append((user_id, path, mode))
        target = self.root / user_id / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if mode == "append" and target.exists():
            target.write_text(target.read_text(encoding="utf-8") + content, encoding="utf-8")
        else:
            target.write_text(content, encoding="utf-8")
        return True, f"已写入 {path}（沙箱）"

    async def patch_file(self, user_id, path, old, new, *, root="", replace_all=False):
        self.patches.append((user_id, path, old))
        target = self.root / user_id / path
        text = target.read_text(encoding="utf-8")
        if replace_all:
            target.write_text(text.replace(old, new), encoding="utf-8")
        else:
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
        return True, f"已修改 {path}（沙箱）"


# ---------------------------------------------------------------- write_file


def test_write_file_dispatches_to_service_backend(ws_root, sandbox_all, monkeypatch):
    """★ 核心：service 后端下 file_write 必须交给后端，不能走 Docker。"""
    from app.sandbox import manager as mgr_mod

    fake = _FakeService(ws_root)
    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: fake)
    refresh("strict", monkeypatch)

    handled, note = asyncio.run(
        mgr_mod.get_manager().write_file("u1", "a.txt", "hello")
    )
    assert fake.writes == [("u1", "a.txt", "overwrite")], (
        f"没有交给 service 后端（writes={fake.writes}）—— write_file 没接分发"
    )
    assert handled is True
    assert (ws_root / "u1" / "a.txt").read_text(encoding="utf-8") == "hello"


def test_write_file_service_failure_respects_strict(ws_root, sandbox_all, monkeypatch):
    """service 后端说失败时，strict 下不许回落进程内。"""
    from app.sandbox import manager as mgr_mod
    from app.sandbox.errors import SandboxUnavailable

    class DownService(_FakeService):
        async def write_file(self, *a, **kw):
            raise SandboxUnavailable("服务挂了")

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: DownService(ws_root))
    refresh("strict", monkeypatch)

    with pytest.raises(SandboxUnavailable):
        asyncio.run(mgr_mod.get_manager().write_file("u1", "b.txt", "x"))
    assert not (ws_root / "u1" / "b.txt").exists(), "strict 下不许回落进程内写"


def test_write_file_service_failure_degrades_in_auto(ws_root, sandbox_all, monkeypatch):
    """auto 下 service 失败 → manager 返回 `handled=False`，由调用方走进程内。

    ## 断言口径

    `manager.write_file` 的职责是「决定谁来做」，**不是**在降级时自己写。
    降级时它返回 `(False, 降级标记)`，真正落盘由 `builtin_tools.file_write`
    的 `_write_impl` 完成。所以这里断言返回值，文件落盘交给下面那条
    `test_builtin_file_write_degrades_and_actually_writes`。
    """
    from app.sandbox import manager as mgr_mod
    from app.sandbox.errors import SandboxUnavailable

    class DownService(_FakeService):
        async def write_file(self, *a, **kw):
            raise SandboxUnavailable("服务挂了")

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: DownService(ws_root))
    refresh("auto", monkeypatch)

    handled, note = asyncio.run(
        mgr_mod.get_manager().write_file("u1", "c.txt", "degraded")
    )
    assert handled is False, "auto 降级时 manager 应返回 handled=False 交给调用方"
    assert "未隔离" in note, f"auto 降级必须留痕，实际：{note!r}"


def test_builtin_file_write_degrades_and_actually_writes(
    ws_root, sandbox_all, monkeypatch
):
    """★ 端到端：auto 降级时，`file_write` 工具必须真的把文件写出来。

    这条补上一条的口子：manager 只负责决策，真正落盘在 builtin 层。
    不看两者的分工，只看**文件在不在**。
    """
    from app import builtin_tools
    from app.sandbox import manager as mgr_mod
    from app.sandbox.errors import SandboxUnavailable

    class DownService(_FakeService):
        async def write_file(self, *a, **kw):
            raise SandboxUnavailable("服务挂了")

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: DownService(ws_root))
    refresh("auto", monkeypatch)

    tools = {t.name: t for t in builtin_tools.build_builtin_tools("u1", include_memory=False)}
    out = str(asyncio.run(tools["file_write"].call({"path": "c.txt", "content": "degraded"})))

    assert "未隔离" in out, f"auto 降级必须留痕，实际：{out[:200]!r}"
    assert (ws_root / "u1" / "c.txt").read_text(encoding="utf-8") == "degraded"


def test_write_file_inproc_does_not_route_to_service(ws_root, sandbox_all, monkeypatch):
    """`backend=inproc` 时**不许**把写操作发给沙箱服务（回滚路径不能变）。

    ## 为什么不断言「抛 SandboxUnavailable」

    第一版这么写，在本机**假红**：本机装了 Docker SDK，`_degrade_reason()`
    返回 None，于是它真的去 `docker run` 了（可能成功、也可能超时），
    根本不会抛。断言「抛异常」把测试绑死在某台机器的 Docker 状态上。

    真正要防的是「inproc 模式下偷偷走了 service 后端」—— 那会让
    回滚路径失去意义。所以直接断言 service 后端的调用记录为空。
    """
    from app.sandbox import manager as mgr_mod

    fake_service = _FakeService(ws_root)

    class InprocMarker:
        name = "inproc"

        async def health(self):
            return True, "ok"

        async def write_file(self, *a, **kw):
            raise AssertionError("inproc 模式不该调用 service 后端的 write_file")

    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: InprocMarker())
    refresh("auto", monkeypatch)

    # 允许它走 Docker 路径失败（本机可能没容器运行时），但**绝不能**是
    # 因为走了 service 后端而失败。
    from app.sandbox.errors import SandboxError

    try:
        asyncio.run(mgr_mod.get_manager().write_file("u1", "d.txt", "x"))
    except SandboxError:
        pass          # Docker 路径失败是可接受的（不计入本测试的判定）
    except AssertionError as exc:
        raise AssertionError(str(exc)) from exc
    assert fake_service.writes == [], "inproc 模式下写操作被路由到了 service 后端"


# ---------------------------------------------------------------- patch_file


def test_patch_file_dispatches_to_service_backend(ws_root, sandbox_all, monkeypatch):
    """★ patch_file 同样必须接 backend 分发（不能只修一半）。"""
    from app.sandbox import manager as mgr_mod

    fake = _FakeService(ws_root)
    monkeypatch.setattr(mgr_mod, "_selected_backend", lambda: fake)
    refresh("strict", monkeypatch)

    (ws_root / "u1").mkdir(parents=True, exist_ok=True)
    (ws_root / "u1" / "p.txt").write_text("old text", encoding="utf-8")

    handled, note = asyncio.run(
        mgr_mod.get_manager().patch_file("u1", "p.txt", "old", "new")
    )
    assert fake.patches == [("u1", "p.txt", "old")], (
        f"没有交给 service 后端（patches={fake.patches}）"
    )
    assert (ws_root / "u1" / "p.txt").read_text(encoding="utf-8") == "new text"


# ---------------------------------------------------------------- 后端协议


def test_service_backend_write_file_is_no_longer_a_stub():
    """`ServiceBackend.write_file` 不能再是 `return False, ""` 的空实现。

    ## 判定方式：看**代码行**，不看整段文本

    第一版写成「整段 body 里不能出现 `return False, ""`」，结果**假红**：
    我的 docstring 里正好引用了那句旧实现作为说明，测试就命中了注释。
    这是本仓库记过的老坑（注释里合法地提到了坏写法）。

    改成：只检查**非注释、非 docstring 的行**是否还残留空实现。
    """
    from app.sandbox.backends import service as svc_mod

    src = Path(svc_mod.__file__).read_text(encoding="utf-8")
    i = src.find("async def write_file")
    assert i > 0, "找不到 write_file"
    end = src.find("async def patch_file", i)
    assert end > i, "找不到 patch_file（write_file 的边界）"

    body = src[i:end]
    # 去掉 docstring（三引号块）与注释行，只看真正的代码
    code_lines = []
    in_doc = False
    for raw in body.splitlines():
        s = raw.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#") or not s:
            continue
        code_lines.append(s)
    code = "\n".join(code_lines)

    assert 'return False, ""' not in code, (
        "ServiceBackend.write_file 仍是空实现 —— 文件工具在 service 后端下会退回进程内"
    )
    assert "/v1/write" in code, "ServiceBackend.write_file 没有真的发请求"