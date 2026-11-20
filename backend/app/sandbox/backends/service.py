"""`service` 后端：HTTP 调独立沙箱服务。

## 身份怎么传

`X-HLZ-User` 是**app 服务端**填的（`user_id` 来自 `builtin_tools.code_run` 的
请求上下文，不是模型的工具参数）。请求体里**不放** `user_id`——
放进去就多了一条可能被篡改的路径（方案 §4.4 约束 1）。

## 降级语义（方案 §6.1）

| mode | 服务不可达时 |
| --- | --- |
| `auto` | 返回 `handled=False` + 首行「本次未隔离」 |
| `strict` | 抛 `SandboxUnavailable` |

## 超时不是降级

服务返回 504 表示「沙箱**执行了**但超时」——这是真实结果，
必须 `handled=True` 返回给模型看，不能当成服务不可用去降级。
把两者混起来会让「代码跑太久」表现为「沙箱没启用」，极难排查。
"""
from __future__ import annotations

import logging
import os

import httpx

from ..errors import SandboxUnavailable

logger = logging.getLogger(__name__)

# 与服务端保持一致（sandbox_service/main.py 的常量）
DEFAULT_SERVICE_TIMEOUT_S = 300.0
CONNECT_TIMEOUT_S = 5.0


class ServiceBackend:
    """调独立沙箱服务执行代码。"""

    name = "service"

    def __init__(self) -> None:
        self._url = (os.environ.get("HLZ_SANDBOX_SERVICE_URL") or "").rstrip("/")
        self._token = (os.environ.get("HLZ_SANDBOX_SERVICE_TOKEN") or "").strip()

    # ------------------------------------------------------------------ 配置

    def _require_config(self) -> None:
        """缺配置时明确报错，不静默降级。

        静默降级会让「服务地址没配」表现为「沙箱一直没生效」，
        而配置错误是部署问题，应该立刻暴露。
        """
        if not self._url:
            raise SandboxUnavailable("未配置 HLZ_SANDBOX_SERVICE_URL")
        if not self._token:
            raise SandboxUnavailable("未配置 HLZ_SANDBOX_SERVICE_TOKEN")

    def _headers(self, user_id: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            # 身份**只走 header**，请求体不放 user_id
            "X-HLZ-User": user_id,
        }

    # ------------------------------------------------------------------ 执行

    async def health(self) -> tuple[bool, str]:
        """打一次 `GET /v1/health`，问服务**自己**能不能干活。

        ## 为什么不能复用 `probe.check`

        `probe.py` 里全是 `docker.from_env()`。本后端走 HTTP 到独立的沙箱服务，
        与 Docker 无关 —— 探 Docker 得到的是「另一件东西」的状态。

        实测两种错法都有：容器里没装 docker SDK → 永远「不可用」→ 每次误降级；
        本机装了 docker SDK → 永远「可用」→ 沙箱真挂了也当没事。

        ## 超时要比执行短得多

        自检是每次调用前的热路径，不能因为服务卡住就让工具调用等 30 秒。
        2 秒足够：连不上就是连不上，慢也是不可用。
        """
        try:
            self._require_config()
        except SandboxUnavailable as exc:
            return False, exc.reason

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(2.0, connect=2.0)) as client:
                resp = await client.get(f"{self._url}/v1/health")
        except Exception as exc:  # noqa: BLE001
            return False, f"{type(exc).__name__}: {exc}"

        if resp.status_code != 200:
            return False, f"健康检查返回 HTTP {resp.status_code}"

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return False, "健康检查返回的不是 JSON"

        if not data.get("ok"):
            return False, f"服务自检失败：{data.get('reason') or '未说明'}"
        return True, f"ok（{data.get('executor', '?')}）"

    async def exec_code(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout: float = 60.0,
        root: str = "",
        session_id: str = "",
        generation_id: str = "",
        agent_id: str = "root",
    ) -> tuple[bool, str, str]:
        from .base import get_backend
        from ..config import get_config

        cfg = get_config()

        try:
            self._require_config()
            payload = {
                "code": code,
                "code_type": code_type,
                "timeout_s": float(timeout),
                "cwd": root or "",
            }
            timeout_cfg = httpx.Timeout(
                DEFAULT_SERVICE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S
            )
            async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                resp = await client.post(
                    f"{self._url}/v1/exec",
                    json=payload,
                    headers=self._headers(user_id),
                )
        except Exception as exc:  # noqa: BLE001
            return self._handle_unavailable(cfg.mode, exc)

        # --- 超时：是「执行过但超时」，不是服务不可用 ---
        if resp.status_code == 504:
            detail = self._extract_detail(resp)
            msg = detail.get("message") or "执行超时"
            stdout = detail.get("stdout") or ""
            stderr = detail.get("stderr") or ""
            body = f"[sandbox] {msg}"
            if stdout:
                body += f"\n{stdout}"
            if stderr:
                body += f"\n{stderr}"
            return True, body, ""

        # --- 4xx：调用方的错，直接报出来（不降级） ---
        if 400 <= resp.status_code < 500:
            detail = self._extract_detail(resp)
            msg = detail.get("message") or detail.get("raw") or str(detail)
            return True, f"[sandbox] 请求被拒绝（HTTP {resp.status_code}）：{msg}", ""

        # --- 5xx：服务出错，按 mode 处理 ---
        if resp.status_code >= 500:
            return self._handle_unavailable(
                cfg.mode, SandboxUnavailable(f"沙箱服务返回 {resp.status_code}")
            )

        data = resp.json()
        out = data.get("stdout") or ""
        err = data.get("stderr") or ""
        exit_code = data.get("exit_code", 0)
        if err:
            out = (out + "\n" + err).strip()
        if exit_code != 0:
            out = f"[sandbox] exit={exit_code}\n{out}".strip()
        return True, out, ""

    # ------------------------------------------------------------------ 辅助

    @staticmethod
    def _extract_detail(resp: httpx.Response) -> dict:
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            return {"raw": resp.text}
        detail = body.get("detail")
        if isinstance(detail, dict):
            return detail
        if isinstance(detail, str):
            return {"message": detail}
        return body if isinstance(body, dict) else {"raw": str(body)}

    @staticmethod
    def _handle_unavailable(mode: str, exc: BaseException) -> tuple[bool, str, str]:
        """服务不可用：`auto` 降级 / `strict` 抛错（方案 §6.1）。"""
        from ..manager import DEGRADED_PREFIX

        reason = f"{type(exc).__name__}: {exc}"
        if mode == "strict":
            raise SandboxUnavailable(reason) from exc
        logger.warning("沙箱服务不可用，降级（mode=auto）：%s", reason)
        return False, DEGRADED_PREFIX.format(reason=reason), ""

    async def write_file(
        self,
        user_id: str,
        path: str,
        content: str,
        *,
        mode: str = "overwrite",
        root: str = "",
    ) -> tuple[bool, str]:
        """把内容写到沙箱侧的 workspace。

        ## 为什么走服务而不是留在进程内

        早期版本这里 `return False, ""`，注释说「文件工具仍在进程内跑
        （方案 §4.2 的该留在 app 的）」。那个出处是错的：方案的 §3.3
        「该留在 app 的」列的是 policy / 工具 schema / 预算 / 降级决策 /
        总开关，**没有文件工具**。

        走服务的收益是防御纵深：写操作也在沙箱的 mount namespace 里，
        万一路径校验有漏洞还有容器兜底。

        ## `root` 怎么处理

        app 侧的 `root` 是「workspace 内的子根」（子代理隔离用，
        如 `.subagents/<session>/<agent>`）。服务侧的契约是**相对
        workspace 根**的路径，所以这里把 root 与 path 拼起来发过去，
        由服务侧统一做越界校验。
        """
        rel = _join_root(root, path)
        payload = {"path": rel, "content": content, "mode": mode}
        return await self._post_file("/v1/write", user_id, payload)

    async def patch_file(
        self,
        user_id: str,
        path: str,
        old: str,
        new: str,
        *,
        root: str = "",
    ) -> tuple[bool, str]:
        """替换文件里的第一处 `old`。"""
        rel = _join_root(root, path)
        payload = {"path": rel, "old": old, "new": new}
        return await self._post_file("/v1/patch", user_id, payload)

    async def _post_file(
        self, endpoint: str, user_id: str, payload: dict
    ) -> tuple[bool, str]:
        """`/v1/write` 与 `/v1/patch` 共用：发请求 + 映射错误。

        错误映射与 `exec_code` 一致：
        - 4xx：调用方的错，直接报出来（**不降级**——参数错不该靠重试解决）
        - 5xx / 连不上：服务不可用，按 mode 决定降级还是抛错
        """
        from ..config import get_config

        cfg = get_config()
        try:
            self._require_config()
            timeout_cfg = httpx.Timeout(DEFAULT_SERVICE_TIMEOUT_S, connect=CONNECT_TIMEOUT_S)
            async with httpx.AsyncClient(timeout=timeout_cfg) as client:
                resp = await client.post(
                    f"{self._url}{endpoint}",
                    json=payload,
                    headers=self._headers(user_id),
                )
        except Exception as exc:  # noqa: BLE001
            handled, note, _ = self._handle_unavailable(cfg.mode, exc)
            return handled, note

        if 400 <= resp.status_code < 500:
            detail = self._extract_detail(resp)
            msg = detail.get("message") or detail.get("raw") or str(detail)
            return True, f"[sandbox] 请求被拒绝（HTTP {resp.status_code}）：{msg}"

        if resp.status_code >= 500:
            handled, note, _ = self._handle_unavailable(
                cfg.mode, SandboxUnavailable(f"沙箱服务返回 {resp.status_code}")
            )
            return handled, note

        try:
            data = resp.json()
        except Exception:  # noqa: BLE001
            return True, "[sandbox] 沙箱服务返回的不是 JSON"

        rel = data.get("path") or payload.get("path") or ""
        chars = data.get("chars", 0)
        if endpoint == "/v1/patch":
            return True, f"已修改 {rel}（{chars} 字符）"
        return True, f"已写入 {rel}（{chars} 字符，mode={payload.get('mode')}）"


def _join_root(root: str, path: str) -> str:
    """把 app 侧的 `root`（workspace 内子根）与 `path` 拼成相对路径。

    ⚠️ 拼接后仍由**服务侧**做越界校验，这里不做安全判断 ——
    两处都判不等于安全翻倍，但一处被判就够。
    """
    r = (root or "").strip().strip("/")
    p = (path or "").strip().lstrip("/")
    return f"{r}/{p}" if r else p