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

## 多实例：每一行都必须能回答「这是哪个实例」（2026-11-19 加）

真跑 `compose --scale app=3` 时暴露一个致命的可观测性缺陷：

**所有副本共享同一个 `appdata` 卷，写同一个 `app.log`。** 三个实例的日志
交错在一起，`tail app.log` 根本看不出某一行是谁打的。排查
「为什么这个请求落到了 B」「B 为什么把 A 的 generation 判死」时无从下手。

三层修法（互相独立，任何一层都能单独定位问题）：

| 层 | 做法 | 回答什么问题 |
| --- | --- | --- |
| 控制台 / app.log | 行首恒定 `[<实例标识>]` | 这一行是哪个实例打的 |
| app.log 文件名 | `app-<实例标识>.log` | 共享卷下也不交错（**关键**） |
| JSONL | `instance` 字段 | 按实例过滤结构化事件 |

实例标识取 `identity._instance_tag()` 的**尾部**（`hostname:` 之后那段），
与 `owner_id()` 和 `X-HLZ-Instance` 响应头的尾部**完全一致** —— 这样日志、
HTTP 响应、Redis 里的 owner 三者能互相印证。

**注入方式很关键**：实例标识加在 **Formatter 层**（以及 record 的 `instance`
属性），绝不改 `record.msg`。否则任何断言日志内容的代码
（`caplog`、`assert "xxx" in caplog.text`）都会因前缀而失败。
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

# 实例标识的前缀格式。放在常量里，测试和文档引用同一个定义。
INSTANCE_PREFIX_FMT = "[%(instance)s] "

_DEFAULT_FORMAT = (
    INSTANCE_PREFIX_FMT + "%(levelname)-7s %(asctime)s %(name)s: %(message)s")
_DATE_FORMAT = "%H:%M:%S"

_configured = False


def instance_tag() -> str:
    """本实例的短标识（owner 的尾部）。

    与 `sandbox.identity.owner_id()`、`X-HLZ-Instance` 响应的尾部一致。
    取不到时返回 `"-"`，绝不抛异常 —— 日志基础设施不该让服务起不来。
    """
    try:
        from .sandbox.identity import _instance_tag
        return _instance_tag()
    except Exception:  # noqa: BLE001
        return "-"


def instance_slug() -> str:
    """日志文件名用的**唯一**标识：`<机器名>-<标识>`。

    ## 为什么不能只用 tag（2026-11-19 真跑 compose 踩到）

    没注入 `HLZ_INSTANCE_ID` 时 tag 就是 PID。两个容器的 python 进程
    **可能拿到同一个 PID**（实测都是 7）—— 于是它们算出同一个文件名
    `app-7.log`，在共享 `appdata` 卷上交错写同一份日志，且无法区分。

    带上机器名（容器 hostname，`--scale` 下天然不同）就唯一了。
    """
    try:
        from .sandbox.identity import _hostname, _instance_tag
        host, tag = _hostname(), _instance_tag()
    except Exception:  # noqa: BLE001
        return "-"
    slug = f"{host}-{tag}"
    return "".join(c if (c.isalnum() or c in "-_.") else "-" for c in slug) or "-"


class InstanceFilter(logging.Filter):
    """给每条 record 补一个 `instance` 属性，供 Formatter 渲染。

    ## 为什么用 Filter 而不是在每条 logger 调用里传 extra

    `logger.info(..., extra={"instance": x})` 要求**每一个调用点**都记得传，
    漏一个就有一行没有归属。Filter 挂在 handler 上是**全局兜底**：
    任何 logger（含第三方库）走这个 handler 都自动带上。

    ## 为什么写在 record 上而不是改 msg

    改 `record.msg` 会污染 `getMessage()`，让所有 `assert "xxx" in caplog.text`
    之类的断言失效。这里只加一个额外属性，消息本身一个字不动。
    """

    def __init__(self, tag: str) -> None:
        super().__init__()
        self.tag = tag

    def filter(self, record: logging.LogRecord) -> bool:
        # 已显式传了 instance 的（比如跨实例转发来的事件）就尊重原值
        if not getattr(record, "instance", ""):
            record.instance = self.tag
        return True


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
            "instance": getattr(record, "instance", "") or instance_tag(),
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
    app_log_path: Path | None = None
    tag = instance_tag()
    if not _configured:
        root.setLevel(level)

        # 实例标识过滤器：挂在每个 handler 上，保证任何 logger（含第三方库）
        # 落到这个 handler 的 record 都带 `instance` 属性。
        inst_filter = InstanceFilter(tag)

        # stdout：人读的格式
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(logging.Formatter(_DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
        console.setLevel(level)
        console.addFilter(inst_filter)
        root.addHandler(console)

        # 可选：应用日志单独落文件（不跟 uvicorn 的 access log 混）
        # 默认开：排查问题时最常见的情况是「事情已经发生了才去看」，
        # 只写到 stdout 的话日志会随终端滚走。
        #
        # ## 多实例下必须按实例分文件
        #
        # 所有副本共享 `appdata` 卷时 `app.log` 会被三个进程交错写，
        # 逻辑行互相穿插（尤其多行 traceback），且无法归属。
        # 加上实例标识后每个实例写自己的文件，`ls logs/` 就能看出跑了几个。
        # 单实例时默认名保持 `app.log`（不破坏现有习惯与文档）。
        app_log = (os.environ.get("HLZ_APP_LOG") or "").strip()
        if not app_log:
            # 没显式指定时按实例分名：`app-<机器名>-<标识>.log`。
            # 必须带机器名：无注入时标识就是 PID，两个容器的 PID 可能相同。
            slug = instance_slug()
            app_log = f"app-{slug}.log" if slug and slug != "-" else "app.log"
        if _env_flag("HLZ_APP_LOG_ON", default=True) and app_log:
            path = Path(app_log) if os.path.isabs(app_log) else _logs_dir() / app_log
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=20 * 1024 * 1024, backupCount=5, encoding="utf-8")
            file_handler.setFormatter(logging.Formatter(
                "%(asctime)s " + _DEFAULT_FORMAT, datefmt=_DATE_FORMAT))
            file_handler.setLevel(level)
            file_handler.addFilter(inst_filter)
            root.addHandler(file_handler)
            app_log_path = path

        # 团队事件：独立 JSONL，便于 grep。默认开。
        if _env_flag("HLZ_TEAM_LOG", default=True):
            team_path = _logs_dir() / f"team-{time.strftime('%Y%m%d')}.jsonl"
            team_path.parent.mkdir(parents=True, exist_ok=True)
            team_handler = logging.handlers.RotatingFileHandler(
                team_path, maxBytes=50 * 1024 * 1024, backupCount=10, encoding="utf-8")
            team_handler.setFormatter(JsonLineFormatter())
            # 团队日志恒为 INFO：它是可观测性的核心，不该被全局级别压掉
            team_handler.setLevel(logging.INFO)
            team_handler.addFilter(inst_filter)
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
            prelude_handler.addFilter(inst_filter)
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
        "instance": tag,
        "team_log": str(team_path) if team_path else "",
        "app_log": str(app_log_path) if app_log_path else "",
        "logs_dir": str(_logs_dir()),
    }
