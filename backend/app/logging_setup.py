"""集中日志配置：让 `logger.info/warning` 真的能看见。

在此之前项目里有 29 处 `logger.*` 调用，但没有一处配置过 handler ——
uvicorn 只配置它自己的 logger，`app.*` 的日志全部被丢弃。结果是排查多智能体
协作问题时只能靠 print 和猜测。

设计要点：

1. **一条命令能开**：`HLZ_LOG_LEVEL=INFO python run.py` 即可看到应用日志。
   默认 `WARNING`，不改变现有运行噪音。
2. **多智能体事件单独成文件**：`HLZ_TEAM_LOG=1` 时，所有 `hlaudz.team` 通道的
   日志额外写到 `backend/data/logs/team-<日期>.jsonl`，每行一条 JSON。
   排查"消息为什么没到""状态为什么不一致"时直接 grep message_id / agent 名。
3. **不引入依赖**：只用标准库 logging + 一个自定义 Formatter。
4. **可轮转**：按大小轮转，避免长期运行把磁盘写满。
"""
from __future__ import annotations

import json
import logging
import logging.handlers
import os
import sys
import time
from pathlib import Path
from typing import Any

from .config import DB_PATH

# 团队协作专用通道：neath 事件都走这个名字，便于单独分流到 JSONL 文件。
TEAM_LOGGER_NAME = "hlaudz.team"

_DEFAULT_FORMAT = "%(levelname)-7s %(asctime)s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def _logs_dir() -> Path:
    return Path(DB_PATH).parent / "logs"


def _env_flag(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in ("1", "true", "yes", "on")


def log_level() -> int:
    raw = (os.environ.get("HLZ_LOG_LEVEL") or "").strip().upper()
    if not raw:
        return logging.WARNING
    return getattr(logging, raw, logging.WARNING)


class JsonLineFormatter(logging.Formatter):
    """把一条日志渲染成单行 JSON。

    排查协作问题时最需要的是**可 grep 的字段**（有哪些 agent、哪条 message_id、
    什么状态），而不是一句自然语言。extra 里传的键会平铺到顶层。
    """

    # LogRecord 自带属性，不该当成业务字段输出
    _RESERVED = frozenset(
        "name msg args levelname levelno pathname filename module exc_info exc_text "
        "stack_info lineno funcName created msecs relativeCreated thread threadName "
        "processName process taskName message asctime".split()
    )

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in self._RESERVED or key.startswith("_"):
                continue
            try:
                json.dumps(value)
                payload[key] = value
            except (TypeError, ValueError):
                payload[key] = str(value)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def team_event(event: str, **fields: Any) -> None:
    """记录一条团队协作事件。

    约定：``event`` 用动词短语（``message_delivered`` / ``agent_idle``），
    ``fields`` 里放可检索的标识（agent / message_id / task_id / status / detail）。
    """
    logging.getLogger(TEAM_LOGGER_NAME).info(event, extra=fields)


def setup_logging() -> dict[str, Any]:
    """幂等地配置根 logger；返回一份实际配置快照，便于启动时打印。"""
    global _configured
    level = log_level()
    root = logging.getLogger()
    team_path: Path | None = None
    if not _configured:
        root.setLevel(level)

        # stdout：人读的格式
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
        console.setLevel(level)
        root.addHandler(console)

        # 可选：应用日志单独落文件（不跟 uvicorn 的 access log 混）
        app_log = (os.environ.get("HLZ_APP_LOG") or "").strip()
        if app_log:
            path = Path(app_log) if os.path.isabs(app_log) else _logs_dir() / app_log
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s " + _DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
            file_handler.setLevel(level)
            root.addHandler(file_handler)

        # 团队事件：独立 JSONL，便于 grep
        if _env_flag("HLZ_TEAM_LOG"):
            team_path = _logs_dir() / f"team-{time.strftime('%Y%m%d')}.jsonl"
            team_path.parent.mkdir(parents=True, exist_ok=True)
            team_handler = logging.handlers.RotatingFileHandler(
                team_path, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8")
            team_handler.setFormatter(JsonLineFormatter())
            # 团队日志恒为 INFO：它是可观测性的核心，不该被全局 WARNING 压掉
            team_handler.setLevel(logging.INFO)
            team_logger = logging.getLogger(TEAM_LOGGER_NAME)
            team_logger.addHandler(team_handler)
            team_logger.setLevel(logging.INFO)
            # 不向上冒泡到 root：否则同一行会同时出现在 console 与文件里，
            # 且 console 上的自然语言形态对 grep 没帮助。
            team_logger.propagate = False

        # 第三方库降噪
        for noisy in ("httpx", "httpcore", "aiosqlite", "fastmcp"):
            logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
        _configured = True

    return {
        "level": logging.getLevelName(level),
        "team_log": str(team_path) if _env_flag("HLZ_TEAM_LOG") and team_path else "",
        "logs_dir": str(_logs_dir()),
    }
