"""集中日志配置：让 `logger.info/warning` 真的能看见。

在此之前项目里有 29 处 `logger.*` 调用，但没有一处配置过 handler ——
uvicorn 只配置它自己的 logger，`app.*` 的日志全部被丢弃。结果是排查多智能体
协作问题时只能靠 print 和猜测。

设计要点：

1. **默认开启**：应用日志默认 `INFO` 并落 `backend/data/logs/app.log`。
   排查多智能体协作问题时，默认静默等于黑盒；要看干净输出用
   `HLZ_LOG_LEVEL=WARNING`（或 `OFF` 完全关掉）。
2. **多智能体事件单独成文件**（默认开启）：所有 `hlaudz.team` 通道的日志写到
   `backend/data/logs/team-<日期>.jsonl`，每行一条 JSON。
   排查"消息为什么没到""状态为什么不一致"时直接 grep message_id / agent 名。
   关闭：`HLZ_TEAM_LOG=0`。
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

# 首帧前奏专用通道：结构化字段（各段耗时）必须能 grep。
# 为什么必须单独一个通道：普通 Formatter 不渲染 `extra`，所以
# `logger.info("...", extra={"soft": 5000})` 落盘后只剩一行人话，
# 数字全丢了。而「首帧慢在哪一段」恰恰只能靠这些字段回答。
PRELUDE_LOGGER_NAME = "hlaudz.prelude"

_DEFAULT_FORMAT = "%(levelname)-7s %(asctime)s %(name)s: %(message)s"
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def _logs_dir() -> Path:
    return Path(DB_PATH).parent / "logs"


def _env_flag(name: str, default: bool = False) -> bool:
    """读布尔开关；未设置时用 default。

    显式关闭（0/false/no/off）必须能覆盖默认值，否则「默认开」就变成了
    「无法关」，长期运行想看干净输出时会很麻烦。
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    text = str(raw).strip().lower()
    if not text:
        return default
    if text in ("0", "false", "no", "off"):
        return False
    return text in ("1", "true", "yes", "on")


def log_level() -> int:
    raw = (os.environ.get("HLZ_LOG_LEVEL") or "").strip().upper()
    if not raw:
        # 默认 INFO：排查多智能体协作问题时没有应用日志基本等于黑盒。
        return logging.INFO
    if raw in ("0", "OFF", "NONE"):
        return logging.CRITICAL
    return getattr(logging, raw, logging.INFO)


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
        # 默认开：排查问题时最常见的情况是「事情已经发生了才去看」，
        # 只写到 stdout 的话日志会随终端滚走。
        app_log = (os.environ.get("HLZ_APP_LOG") or "").strip() or "app.log"
        if _env_flag("HLZ_APP_LOG_ON", default=True) and app_log:
            path = Path(app_log) if os.path.isabs(app_log) else _logs_dir() / app_log
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(
                logging.Formatter("%(asctime)s " + _DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
            file_handler.setLevel(level)
            root.addHandler(file_handler)

        # 团队事件：独立 JSONL，便于 grep。默认开。
        if _env_flag("HLZ_TEAM_LOG", default=True):
            team_path = _logs_dir() / f"team-{time.strftime('%Y%m%d')}.jsonl"
            team_path.parent.mkdir(parents=True, exist_ok=True)
            team_handler = logging.handlers.RotatingFileHandler(
                team_path, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8")
            team_handler.setFormatter(JsonLineFormatter())
            # 团队日志恒为 INFO：它是可观测性的核心，不该被全局级别压掉
            team_handler.setLevel(logging.INFO)
            team_logger = logging.getLogger(TEAM_LOGGER_NAME)
            team_logger.addHandler(team_handler)
            team_logger.setLevel(logging.INFO)
            # 不向上冒泡到 root：否则同一行会同时出现在 console 与文件里，
            # 且 console 上的自然语言形态对 grep 没帮助。
            team_logger.propagate = False

        # 首帧前奏：独立 JSONL（字段可 grep），同时保留 root 上的人话版本。
        # 两者并存是有意的：
        #   - JSONL 回答「soft 段花了多少 ms」这类精确问题；
        #   - root 的人话行让 `tail app.log` 时也能看出发生过一轮。
        if _env_flag("HLZ_PRELUDE_LOG_FILE", default=True):
            prelude_path = _logs_dir() / f"prelude-{time.strftime('%Y%m%d')}.jsonl"
            prelude_path.parent.mkdir(parents=True, exist_ok=True)
            prelude_handler = logging.handlers.RotatingFileHandler(
                prelude_path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
            prelude_handler.setFormatter(JsonLineFormatter())
            prelude_handler.setLevel(logging.INFO)
            prelude_logger = logging.getLogger(PRELUDE_LOGGER_NAME)
            prelude_logger.addHandler(prelude_handler)
            prelude_logger.setLevel(logging.INFO)
            # 允许冒泡：root 上那条人话行是给 `tail` 用的，成本可忽略。
            prelude_logger.propagate = True

        # 第三方库降噪
        for noisy in ("httpx", "httpcore", "aiosqlite", "fastmcp"):
            logging.getLogger(noisy).setLevel(max(level, logging.WARNING))
        _configured = True

    return {
        "level": logging.getLevelName(level),
        "team_log": str(team_path) if team_path else "",
        "logs_dir": str(_logs_dir()),
    }
