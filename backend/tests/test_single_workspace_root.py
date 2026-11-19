"""单根收敛：workspace 只能有一个根（TDD RED）。

## 为什么要收敛（实测到的生产错位，不是理论问题）

收敛前是「两个根 + 一个白名单」：

```
if user_id in SANDBOX_WORKSPACE_USERS:
    return SANDBOX_WORKSPACES_DIR / user_id   # sandbox/
return WORKSPACES_DIR / user_id               # workspaces/
```

而 `HLZ_SANDBOX_MVP_USERS` 在**两个容器里默认值不同**（compose 里 app 用
`${VAR-}` 空默认、sandbox 用 `${VAR-u_xxx,u_yyy}` 白名单默认），于是
同一个用户在两边的路径不一致。实证（2026-11-19，hlzsbxprobe 栈）：

    app     thinks u_c56b8337dd09 -> /app/backend/data/workspaces/u_c56b8337dd09
    sandbox thinks u_c56b8337dd09 -> /app/backend/data/sandbox/u_c56b8337dd09

这就是设计文档 `20261118` §6.3 警告的「file_write 写进 A、file_read 读 B」
两套视图错位 —— 它现在就在生产里活着，只是恰好白名单用户没用过。

## 为什么这个分支本来就没理由

双根是 **Docker 方案**的遗留：那时沙箱 = 一个容器，需要 `-v D:\sandbox\<uid>:/ws`
挂一个「容器专属」目录。

bwrap 用 `--bind <workspace> /ws`，**同一个路径进出**，没有「容器侧目录」
这个概念。`workspaces/` 下的目录一样能 bind。所以：

| 双根给出的好处 | bwrap 下是否成立 |
| --- | --- |
| 容器需要独立的挂载源 | ❌ 不需要，bind 任意目录都行 |
| `D:\sandbox` 不占 C 盘 | ❌ 容器里两个根都在同一个 appdata 卷上 |
| 白名单用户隔离数据 | ❌ 隔离靠 `--bind` 单目录，与目录名无关 |

而它带来的成本是真实的：两个根要同步、两个环境变量要配对、
同名的「空」在 `user_in_scope` 与 `workspaces_root` 里含义相反（已踩过）。

## 收敛后的不变量

**一个用户，一个根，不受任何沙箱开关影响。**
沙箱开关（MODE / BACKEND / MVP_USERS）只决定「走不走沙箱」，
**不决定文件放哪**。这条分开是本次收敛的核心。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"

if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


# ---------------------------------------------------------------- 1. 只有一个根


def test_workspaces_root_is_single_root(tmp_path, monkeypatch):
    """任意用户都落在同一个根下。"""
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    for uid in ("u_any", "u_c56b8337dd09", "u_343a46896bff", "u_brand_new"):
        assert ws_mod.workspaces_root(uid) == tmp_path / uid


def test_whitelist_does_not_affect_path(tmp_path, monkeypatch):
    """★ 核心：把用户放进白名单，路径**不能**变。

    这条直接钉死本次事故的根因 —— 白名单只该决定「走不走沙箱」，
    不该决定「文件放哪」。
    """
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    uid = "u_c56b8337dd09"

    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")
    before = ws_mod.workspaces_root(uid)

    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", uid)
    after = ws_mod.workspaces_root(uid)

    assert before == after == tmp_path / uid, (
        f"白名单改变了路径：空={before} 含该用户={after}。"
        f"这正是 app 与 sandbox 容器算出不同目录的原因。"
    )


def test_sandbox_root_names_are_gone():
    """`SANDBOX_WORKSPACES_DIR` / `SANDBOX_WORKSPACE_USERS` 必须彻底不存在。

    留着它们就等于留着「再有人用一下」的机会。收敛要彻底。
    """
    from app import config as cfg

    for name in ("SANDBOX_WORKSPACES_DIR", "SANDBOX_WORKSPACE_USERS"):
        assert not hasattr(cfg, name), (
            f"app.config 里还有 {name} —— 双根没删干净"
        )
        assert not hasattr(cfg, "default_sandbox_root"), (
            "app.config 里还有 default_sandbox_root —— 双根的默认值逻辑没删干净"
        )


def test_sandbox_service_has_no_dual_root():
    """沙箱服务侧同样不许有双根逻辑。"""
    from sandbox_service import workspace as sws

    for name in (
        "SANDBOX_WORKSPACES_ENV",
        "DEFAULT_SANDBOX_WORKSPACES_DIR",
        "sandbox_workspaces_dir",
        "mvp_users",
    ):
        assert not hasattr(sws, name), f"sandbox_service.workspace 里还有 {name}"


# ---------------------------------------------------------------- 2. 两侧一致


def test_both_sides_agree_for_any_user(tmp_path, monkeypatch):
    """★ app 与沙箱服务对同一用户必须算出**完全相同**的路径。

    只要这条成立，就不可能再出现「写进 A、读 B」。
    """
    from app import workspace as app_ws
    from sandbox_service import workspace as svc_ws

    monkeypatch.setattr(app_ws, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setenv("HLZ_WORKSPACES_DIR", str(tmp_path))

    for uid in ("u_a", "u_c56b8337dd09", "u_new_user"):
        a = app_ws.workspaces_root(uid)
        b = svc_ws.workspaces_root(uid)
        assert a == b, f"{uid}: app={a} sandbox={b}"


def test_both_sides_agree_regardless_of_whitelist(tmp_path, monkeypatch):
    """即使两侧的 `HLZ_SANDBOX_MVP_USERS` 配置不同，路径也必须一致。

    这条是本次事故的直接回归测试：事故的成因正是两侧该变量默认值不同。
    """
    from app import workspace as app_ws
    from sandbox_service import workspace as svc_ws

    monkeypatch.setattr(app_ws, "WORKSPACES_DIR", tmp_path)
    monkeypatch.setenv("HLZ_WORKSPACES_DIR", str(tmp_path))
    uid = "u_c56b8337dd09"

    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", "")          # app 侧那种
    a = app_ws.workspaces_root(uid)

    monkeypatch.setenv("HLZ_SANDBOX_MVP_USERS", f"u_x,{uid}")  # sandbox 侧那种
    b = svc_ws.workspaces_root(uid)

    assert a == b == tmp_path / uid


# ---------------------------------------------------------------- 3. 部署侧


def test_compose_has_no_sandbox_workspaces_dir():
    """compose 里不许再出现 `HLZ_SANDBOX_WORKSPACES_DIR`。

    每多一个「两边都要同步」的变量，就多一次漂移机会 ——
    本次事故就是这么来的。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    live = [
        line for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    offenders = [l.strip() for l in live if "HLZ_SANDBOX_WORKSPACES_DIR" in l]
    assert not offenders, f"compose 里还有双根变量：{offenders}"


def test_compose_mvp_users_has_identical_default_on_both_services():
    """★ 事故直接成因：同一个变量在两个服务上默认值不同。

    app 写 `${HLZ_SANDBOX_MVP_USERS-}`（空默认），
    sandbox 写 `${HLZ_SANDBOX_MVP_USERS-u_xxx,u_yyy}`（白名单默认）。
    compose 的插值发生在**宿主**，两边各自求值 → 容器里看到不同的值。

    即使单根收敛后这个变量不再影响路径，它仍然影响「谁能进沙箱」，
    两侧不一致会造成「app 认为该走沙箱、sandbox 不认」这类问题。
    所以要求两个服务上的写法**逐字相同**。
    """
    text = COMPOSE.read_text(encoding="utf-8")
    live = [
        line.strip() for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    settings = [l for l in live if l.startswith("HLZ_SANDBOX_MVP_USERS:")]
    assert len(settings) == 2, f"期望两个服务各一条，实际 {len(settings)}：{settings}"
    assert settings[0] == settings[1], (
        f"两侧默认值写法不同：\n  {settings[0]}\n  {settings[1]}\n"
        f"compose 的 ${{VAR-default}} 各自求值，会导致两个容器看到不同的值。"
    )


# ---------------------------------------------------------------- 4. 平台


def test_no_backslash_in_any_user_path(tmp_path, monkeypatch):
    """Linux 上算出的路径不许含反斜杠。

    历史事故（`20261119`）：默认根写死 `D:\\sandbox`，在 Linux 容器里
    变成相对路径，拼出 `/app/backend/D:\\sandbox/<uid>` 这种畸形目录。
    收敛后这个默认值没了，但保留这条断言防止将来有人再加一个平台特判。
    """
    from app import workspace as ws_mod

    monkeypatch.setattr(ws_mod, "WORKSPACES_DIR", tmp_path)
    got = str(ws_mod.workspaces_root("u_test"))
    assert "\\" not in got or sys.platform == "win32", got