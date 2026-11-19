"""部署配置的静态守卫：把「首次真跑 compose 才暴露」的四个坑钉住。

## 为什么需要静态测试

这四个问题都是**部署配置写错了但代码完全正常**：

1. 没 `.dockerignore` -> 构建失败（346 MB 上下文 + 异常权限位）
2. compose `context: ..` -> `"/backend": not found`
3. `nginx.conf` 缺 `events/http` 外壳 -> nginx 进重启循环
4. `hash ... consistent` + `server app:8000` -> **粘性静默失效**

前三个会「跑不起来」，第四个最危险 —— 服务全绿、日志无错，只是每个请求
都可能落到不同实例。这类只能靠静态断言 + 真机 E2E 共同覆盖。

## 反向确认

- 删掉 `.dockerignore` -> `test_dockerignore_excludes_runtime_data` 变红
- 把 `context` 改回 `..` -> `test_compose_context_points_at_repo_root` 变红
- 删掉 `nginx.conf` 的 `http {` -> `test_nginx_conf_is_full_main_config` 变红
- 把 `hash` 改回带 `consistent` -> `test_nginx_hash_is_not_consistent` 变红
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
COMPOSE = BACKEND / "deploy" / "docker-compose.yml"
NGINX = BACKEND / "deploy" / "nginx.conf"
DOCKERIGNORE = REPO / ".dockerignore"


def live_lines(path: Path) -> list[tuple[int, str]]:
    """返回非注释、非空的行（行号 + 去缩进内容）。

    注释里当然会提到错误写法（比如「不要写 ${HOSTNAME}」），
    只断言生效行，否则会因为「有人写了正确的反例注释」而误红。
    """
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append((n, s))
    return out


# ---------------------------------------------------------------- .dockerignore

def test_dockerignore_exists():
    """没有它时构建上下文 346 MB，且 data/ 里有异常权限位直接构建失败。"""
    assert DOCKERIGNORE.exists(), (
        "仓库根没有 .dockerignore —— .gitignore 对 docker build 无效，"
        "backend/data/（用户数据）会被打进构建上下文")


@pytest.mark.parametrize("pattern", [
    "backend/data/",
    "backend/tests/",
    "backend/work/",
])
def test_dockerignore_excludes_runtime_data(pattern: str):
    text = DOCKERIGNORE.read_text(encoding="utf-8")
    live = [ln.strip() for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("#")]
    assert pattern in live, f".dockerignore 没有排除 {pattern}"


# ---------------------------------------------------------------- compose 上下文

def test_compose_context_points_at_repo_root():
    """`context: ..` 是相对 deploy/ 解析的 -> 指向 backend/，
    而 Dockerfile 写的是 `COPY backend/...`，所以必须是 `../..`。"""
    live = live_lines(COMPOSE)
    ctx = [s for _, s in live if s.startswith("context:")]
    assert ctx, "compose 没有 build.context"
    value = ctx[0].split(":", 1)[1].strip()
    assert value == "../..", (
        f"compose build.context={value!r}，应指向仓库根（../..）")


def test_compose_dockerfile_path_matches_context():
    """`dockerfile` 相对**上下文**解析，所以要从仓库根写全路径。"""
    live = live_lines(COMPOSE)
    df = [s for _, s in live if s.startswith("dockerfile:")]
    assert df, "compose 没有 build.dockerfile"
    value = df[0].split(":", 1)[1].strip()
    assert value == "backend/deploy/Dockerfile", (
        f"dockerfile={value!r}，上下文是仓库根，路径应为 backend/deploy/Dockerfile")
    assert (REPO / value).exists(), f"{value} 不存在"


# ---------------------------------------------------------------- nginx 主配置

def test_nginx_conf_is_full_main_config():
    """挂到 /etc/nginx/nginx.conf 的必须是**完整主配置**。

    缺 `events {}` / `http {}` 会 `"upstream" directive is not allowed here`
    并进重启循环（实测踩过）。
    """
    live = [s for _, s in live_lines(NGINX)]
    joined = "\n".join(live)
    assert re.search(r"^events\s*\{", joined, re.M), "nginx.conf 缺 events {} 块"
    assert re.search(r"^http\s*\{", joined, re.M), "nginx.conf 缺 http {} 块"


def test_nginx_upstream_inside_http():
    """`upstream` / `map` / `server` 只能在 `http {}` 里面。"""
    text = NGINX.read_text(encoding="utf-8")
    http_at = text.find("\nhttp {")
    assert http_at >= 0, "找不到 http {"
    for directive in ("upstream hlaudz_app {", "map $upstream_status",
                      "server {\n        listen 80;"):
        pos = text.find(directive)
        assert pos > http_at, f"`{directive.split(chr(10))[0]}` 出现在 http 块之外"


# ---------------------------------------------------------------- 粘性（关键）

def test_nginx_hash_is_not_consistent():
    """**核心**：`hash ... consistent` + `server app:8000` 会让粘性完全失效。

    ketama 环按 server **名字**建点；`--scale app=3` 下 DNS 返回 3 个 A 记录，
    但三个 peer 共享同一个名字 `app:8000`，环上三点重合 -> 退化成严格轮询。
    实测：同一 cookie 连发 12 次，三个副本各 4 次。

    修法二选一：普通 `hash`（本项目的选择），或显式列出每个不同名的 peer。
    真机验证见 `tests/manual/compose_scale_e2e.py`。
    """
    live = live_lines(NGINX)
    hash_lines = [(n, s) for n, s in live if s.startswith("hash ")]
    assert hash_lines, "nginx.conf 里没有 hash 指令（粘性完全没配）"
    for n, s in hash_lines:
        assert "consistent" not in s, (
            f"deploy/nginx.conf:{n} 用了 `{s}`；"
            "`consistent` 在 `server app:8000`（DNS 多 A、同名 peer）下"
            "会退化成轮询，粘性失效。去掉 consistent，或显式列出每个副本")


def test_nginx_has_single_app_server_or_explicit_peers():
    """如果只有一条 `server app:8000`，那 `hash` 必须是普通模式。

    显式列出多个不同名 peer 时 `consistent` 可以有意义 —— 但那种写法下
    每个 peer 的名字不同，上面的断言不会针对它（因为它仍然是一条 hash 行）。
    所以这条只做提示性检查：确认 upstream 里确实有 server。
    """
    live = [s for _, s in live_lines(NGINX)]
    servers = [s for s in live if s.startswith("server ") and ":8000" in s]
    assert servers, "upstream 里没有 :8000 的 server"


# ---------------------------------------------------------------- 粘性 cookie

def test_sticky_cookie_name_matches_app():
    """nginx 读的 cookie 名必须与应用下发的一致。"""
    live_text = "\n".join(s for _, s in live_lines(NGINX))
    m = re.search(r"hash \$cookie_(\w+)", live_text)
    assert m, "nginx.conf 没有 hash $cookie_xxx"
    nginx_cookie = m.group(1)

    main_py = (BACKEND / "app" / "main.py").read_text(encoding="utf-8")
    m2 = re.search(r'STICKY_COOKIE\s*=\s*"([^"]+)"', main_py)
    assert m2, "main.py 里没有 STICKY_COOKIE"
    assert m2.group(1) == nginx_cookie, (
        f"cookie 名不一致：nginx 读 {nginx_cookie!r}，应用下发 {m2.group(1)!r}")