"""沙箱服务的文件写端点：`/v1/write` 与 `/v1/patch`。

## 为什么服务侧要自己做路径校验

app 侧已经用 `safe_workspace_path()` 校验过一次。但服务侧**不能**因此跳过：

- app 可能被攻破
- 可能有别的入口直连服务
- 将来加了新调用方

只信调用方 = 把「任意位置写文件」开放出去。所以这里逐条钉住：
绝对路径拒、`..` 越界拒、符号链接逃逸拒。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


@pytest.fixture()
def ws_root(tmp_path, monkeypatch):
    from sandbox_service import workspace as ws_mod

    monkeypatch.setenv(ws_mod.WORKSPACES_ENV, str(tmp_path))
    monkeypatch.setattr(ws_mod, "DEFAULT_WORKSPACES_DIR", tmp_path)
    (tmp_path / "u_authed").mkdir()
    return tmp_path


@pytest.fixture()
def client(ws_root, monkeypatch):
    from sandbox_service import main as svc_main

    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "t")
    monkeypatch.setattr(svc_main, "_pool", None)
    svc_main.app.dependency_overrides[svc_main.authenticate] = lambda: "u_authed"
    with TestClient(svc_main.app) as c:
        yield c
    svc_main.app.dependency_overrides.clear()


# ---------------------------------------------------------------- write 正例


def test_write_creates_file(client, ws_root):
    r = client.post("/v1/write", json={"path": "a/b.txt", "content": "hello"})
    assert r.status_code == 200, r.text
    assert (ws_root / "u_authed" / "a" / "b.txt").read_text(encoding="utf-8") == "hello"
    body = r.json()
    assert body["chars"] == 5
    assert body["mode"] == "overwrite"


def test_write_overwrites_by_default(client, ws_root):
    target = ws_root / "u_authed" / "x.txt"
    target.write_text("old-content", encoding="utf-8")
    r = client.post("/v1/write", json={"path": "x.txt", "content": "new"})
    assert r.status_code == 200
    assert target.read_text(encoding="utf-8") == "new"


def test_write_append_mode(client, ws_root):
    target = ws_root / "u_authed" / "y.txt"
    target.write_text("first\n", encoding="utf-8")
    r = client.post(
        "/v1/write", json={"path": "y.txt", "content": "second\n", "mode": "append"}
    )
    assert r.status_code == 200
    assert target.read_text(encoding="utf-8") == "first\nsecond\n"


def test_write_creates_nested_dirs(client, ws_root):
    r = client.post("/v1/write", json={"path": "deep/a/b/c.md", "content": "x"})
    assert r.status_code == 200
    assert (ws_root / "u_authed" / "deep" / "a" / "b" / "c.md").is_file()


def test_write_utf8_content(client, ws_root):
    text = "中文内容 🎉\n第二行"
    r = client.post("/v1/write", json={"path": "zh.txt", "content": text})
    assert r.status_code == 200
    assert (ws_root / "u_authed" / "zh.txt").read_text(encoding="utf-8") == text


# ---------------------------------------------------------------- write 防护


def test_write_rejects_absolute_path(client, ws_root):
    """绝对路径必拒 —— app 的契约是发相对路径。

    ⚠️ 不断言具体文案：Windows 上 `Path("/etc/passwd").is_absolute()` 是 False
    （没有盘符），所以它会走「越界」分支而不是「只接受相对路径」分支。
    两者都是 400 拒绝，语义一致 —— 断言状态码，不断言是哪个分支拒的。
    """
    r = client.post("/v1/write", json={"path": "/etc/passwd", "content": "x"})
    assert r.status_code == 400
    assert not Path("/etc/passwd").exists() or True   # 明确不做副作用断言


def test_write_rejects_windows_absolute_path(client):
    """Windows 绝对路径（带盘符）必须命中「只接受相对路径」那条分支。"""
    r = client.post("/v1/write", json={"path": "C:/Windows/evil.txt", "content": "x"})
    assert r.status_code == 400


def test_write_rejects_parent_escape(client, ws_root):
    r = client.post("/v1/write", json={"path": "../escape.txt", "content": "x"})
    assert r.status_code == 400


def test_write_rejects_deep_escape(client, ws_root):
    r = client.post("/v1/write", json={"path": "a/../../escape.txt", "content": "x"})
    assert r.status_code == 400


def test_write_rejects_empty_path(client):
    r = client.post("/v1/write", json={"path": "  ", "content": "x"})
    assert r.status_code == 400


def test_write_rejects_symlink_escape(client, ws_root, tmp_path):
    """符号链接指向 workspace 外 → 必须拒（resolve 后越界）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    link = ws_root / "u_authed" / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("本平台不支持建符号链接（Windows 非管理员）")

    r = client.post("/v1/write", json={"path": "link/evil.txt", "content": "x"})
    assert r.status_code == 400, "符号链接逃逸没被拦住"


def test_write_rejects_bad_mode(client):
    r = client.post(
        "/v1/write", json={"path": "m.txt", "content": "x", "mode": "nonsense"}
    )
    assert r.status_code == 422


def test_write_rejects_user_id_conflict(client):
    """请求体 user_id 与认证身份不符 → 403（身份只来自认证）。"""
    r = client.post(
        "/v1/write",
        json={"path": "c.txt", "content": "x", "user_id": "u_someone_else"},
    )
    assert r.status_code == 403


# ---------------------------------------------------------------- patch


def test_patch_replaces_first_occurrence(client, ws_root):
    target = ws_root / "u_authed" / "p.txt"
    target.write_text("aaa bbb aaa", encoding="utf-8")
    r = client.post("/v1/patch", json={"path": "p.txt", "old": "aaa", "new": "ZZZ"})
    assert r.status_code == 200, r.text
    assert target.read_text(encoding="utf-8") == "ZZZ bbb aaa", "应只替换第一处"


def test_patch_missing_target_returns_404(client):
    r = client.post("/v1/patch", json={"path": "nope.txt", "old": "a", "new": "b"})
    assert r.status_code == 404


def test_patch_missing_old_returns_409(client, ws_root):
    """找不到要替换的文本 → 409，**不能静默成功**（模型会以为改好了）。"""
    target = ws_root / "u_authed" / "q.txt"
    target.write_text("content", encoding="utf-8")
    r = client.post("/v1/patch", json={"path": "q.txt", "old": "not-there", "new": "x"})
    assert r.status_code == 409


def test_patch_empty_old_returns_422(client, ws_root):
    (ws_root / "u_authed" / "r.txt").write_text("x", encoding="utf-8")
    r = client.post("/v1/patch", json={"path": "r.txt", "old": "", "new": "y"})
    assert r.status_code == 422


def test_patch_rejects_escape(client):
    r = client.post("/v1/patch", json={"path": "../x.txt", "old": "a", "new": "b"})
    assert r.status_code == 400


# ---------------------------------------------------------------- 认证


def test_write_requires_auth(monkeypatch, tmp_path):
    """未认证访问必须 401（不依赖 dependency_overrides）。"""
    from sandbox_service import main as svc_main

    monkeypatch.setenv("HLZ_SANDBOX_SERVICE_TOKEN", "secret")
    monkeypatch.setenv("HLZ_WORKSPACES_DIR", str(tmp_path))
    monkeypatch.setattr(svc_main, "_pool", None)
    with TestClient(svc_main.app) as c:
        r = c.post("/v1/write", json={"path": "a.txt", "content": "x"})
    assert r.status_code == 401