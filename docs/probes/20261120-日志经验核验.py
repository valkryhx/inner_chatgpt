"""校验《Python 日志静默丢失排查经验》里的事实。

## 为什么需要

那份文档通篇是「Python/uvicorn 的行为是这样」——每条都是**可验证的机制断言**，
而不是"我记得如此"。这类文档最容易腐烂：写的时候对，半年后库版本变了、
或者自己记错了，读者照着排查就被带偏。

## 校验什么

1. uvicorn 的 `LOGGING_CONFIG` 结构（没有 root 键、只配 3 个 logger）
2. Python 的默认状态（root = WARNING、handlers 空、lastResort 存在）
3. 文档说的「`--log-level` 救不了」这个否定结论
4. 文档自身：订正声明、代码示例、排查清单都在

用法：
    python docs/probes/20261120-日志经验核验.py
"""
from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path

REPO = Path(os.environ.get("HLZ_LOGEXP_REPO")
            or Path(__file__).resolve().parents[2])
DOC = Path(os.environ.get("HLZ_LOGEXP_DOC")
           or (REPO / "docs" / "20261120-Python日志静默丢失排查经验.md"))

FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    mark = "[OK]  " if cond else "[FAIL]"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def must_not(name: str, cond_absent: bool, detail: str = "") -> None:
    """否定结论：条件为真 = 通过（调用方传 `not in` 形式）。"""
    check(f"[must_not] {name}", cond_absent, detail)


def main() -> int:
    print("=" * 70)
    print("一、uvicorn 的实际行为（文档 §2.1）")
    print("=" * 70)

    try:
        from uvicorn.config import LOGGING_CONFIG
    except ImportError:
        print("  [SKIP] 本机没装 uvicorn")
        return 0

    check("LOGGING_CONFIG 里**没有** root 键",
          "root" not in LOGGING_CONFIG,
          f"实际 keys: {sorted(LOGGING_CONFIG)}")
    loggers = set(LOGGING_CONFIG.get("loggers", {}))
    check("只配了 3 个 uvicorn* logger",
          loggers == {"uvicorn", "uvicorn.access", "uvicorn.error"},
          f"实际: {sorted(loggers)}")
    check("disable_existing_loggers 是 False（不会破坏已有 logger）",
          LOGGING_CONFIG.get("disable_existing_loggers") is False,
          str(LOGGING_CONFIG.get("disable_existing_loggers")))

    print()
    print("=" * 70)
    print("二、Python 的默认状态（文档 §2.2）")
    print("=" * 70)

    root = logging.getLogger()
    check("root 默认 level 是 WARNING",
          root.level == logging.WARNING,
          logging.getLevelName(root.level))
    check("root 默认没有 handler",
          root.handlers == [], str(root.handlers))
    check("logging.lastResort 存在", logging.lastResort is not None)
    check("lastResort 的 level 是 WARNING",
          logging.lastResort.level == logging.WARNING,
          logging.getLevelName(logging.lastResort.level) if logging.lastResort else "-")

    # 未配置的 logger：level=NOTSET、propagate=True
    probe = logging.getLogger("hlz_probe.unconfigured")
    check("未配置 logger 的 level 是 NOTSET",
          probe.level == logging.NOTSET, logging.getLevelName(probe.level))
    check("未配置 logger 的 handlers 为空", probe.handlers == [])
    check("未配置 logger 默认 propagate=True", probe.propagate is True)

    print()
    print("=" * 70)
    print("三、决定性行为：INFO 会被丢弃，WARNING 不会")
    print("=" * 70)

    # 用一个隔离的 logger + 捕获 handler 来验证「level 判定在 handler 之前」
    iso = logging.getLogger("hlz_probe.isolation")
    iso.handlers.clear()
    iso.propagate = False
    iso.setLevel(logging.NOTSET)          # 继承 root(WARNING)

    captured: list[str] = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    h = Capture()
    h.setLevel(logging.DEBUG)             # handler 允许 DEBUG
    iso.addHandler(h)

    iso.info("SHOULD_BE_DROPPED")
    check("INFO 被丢弃（即便 handler 允许 DEBUG）",
          "SHOULD_BE_DROPPED" not in captured, str(captured))

    iso.warning("SHOULD_PASS")
    check("WARNING 能通过", "SHOULD_PASS" in captured, str(captured))

    # 设了 level 之后 INFO 就能过
    iso.setLevel(logging.INFO)
    iso.info("NOW_VISIBLE")
    check("logger 设成 INFO 后，INFO 能通过",
          "NOW_VISIBLE" in captured, str(captured))

    iso.handlers.clear()

    print()
    print("=" * 70)
    print("四、文档自身")
    print("=" * 70)

    doc = DOC.read_text(encoding="utf-8")
    check("文档存在且非空", len(doc) > 3000, f"{len(doc)} 字符")
    check("文档标注了实测（不是推测）", "实测" in doc)

    # 核心结论必须都在
    #
    # ⚠️ 这里的字符串必须**逐字**抄文档里的原文（AGENTS.md §8.6 规则 2）。
    # 第一次写「not uvicorn 的 bug」，而文档里是「不是 uvicorn 的 bug」——
    # 英文 not 换成了中文「不是」，断言假红。
    # 教训：断言串要从文档里复制，不要凭记忆重写。
    for key in ("root logger 默认 level = WARNING",
                "--log-level info` 只调整 uvicorn",
                "不是 uvicorn 的 bug"):
        check(f"文档含关键结论：{key}", key in doc)

    # 订正声明（第一版写成「uvicorn 的 bug」是错的，必须留痕）
    check("文档含订正声明", "订正是" in doc or "订正记在这里" in doc)
    must_not("文档没有把结论写成「uvicorn 的 bug」",
             "uvicorn 不给 app logger 配 handler 是个 bug" not in doc
             or "这是错的" in doc)

    # 三件必须做的事
    check("文档要求配包根 logger", "包根" in doc)
    check("文档要求 if root.handlers 幂等", "if root.handlers" in doc)
    check("文档要求 level 可配", "LOG_LEVEL" in doc)
    # 反面做法
    check("文档说明 basicConfig 不适合", "basicConfig" in doc)
    # 排查清单
    check("文档有排查清单", "排查清单" in doc)

    print()
    print("=" * 70)
    if FAILS:
        print(f"结果：{len(FAILS)} 项失败")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())