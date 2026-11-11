"""日志基础设施：确保 `logger.info` 真的能看见，团队事件能落 JSONL。

背景：这个模块存在之前，项目里有 29 处 `logger.*` 调用但**没有配置过任何 handler**，
uvicorn 只管自己的 logger，`app.*` 的日志全部被静默丢弃。
"""
from __future__ import annotations

import json
import logging
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import logging_setup  # noqa: E402


@pytest.fixture
def fresh_logging(monkeypatch, tmp_path):
    """重置全局配置，让每个用例都能重新 setup 到临时目录。"""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    team_logger = logging.getLogger(logging_setup.TEAM_LOGGER_NAME)
    saved_team = list(team_logger.handlers)
    saved_prop = team_logger.propagate
    logging_setup._configured = False
    monkeypatch.setattr(logging_setup, "_logs_dir", lambda: tmp_path)
    yield tmp_path
    root.handlers[:] = saved_handlers
    root.setLevel(saved_level)
    team_logger.handlers[:] = saved_team
    team_logger.propagate = saved_prop
    logging_setup._configured = False


def test_app_logger_gets_a_handler(fresh_logging, monkeypatch, capfd):
    """核心回归：应用 logger 必须真的输出，而不是被静默丢弃。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    logging_setup.setup_logging()

    logging.getLogger("app.demo").info("协作问题排查信息")

    out = capfd.readouterr()
    # StreamHandler 默认写 stderr；两者都看，避免实现细节影响断言
    assert "协作问题排查信息" in (out.out + out.err)


def test_default_level_is_warning(fresh_logging, monkeypatch):
    """不设环境变量时保持安静，不改变现有运行噪音。"""
    monkeypatch.delenv("HLZ_LOG_LEVEL", raising=False)
    cfg = logging_setup.setup_logging()
    assert cfg["level"] == "WARNING"


def test_team_event_writes_jsonl(fresh_logging, monkeypatch):
    monkeypatch.setenv("HLZ_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("HLZ_TEAM_LOG", "1")
    cfg = logging_setup.setup_logging()
    assert cfg["team_log"], "开启 HLZ_TEAM_LOG 后应给出文件路径"

    logging_setup.team_event("message_delivered", agent="alpha",
                             message_id="tmsg_1", was_idle=True)

    path = fresh_logging / f"team-{__import__('time').strftime('%Y%m%d')}.jsonl"
    assert path.exists(), f"应写出 {path}"
    row = json.loads(path.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert row["event"] == "message_delivered"
    assert row["agent"] == "alpha"
    assert row["message_id"] == "tmsg_1"
    assert row["was_idle"] is True
    assert row["level"] == "INFO"


def test_team_log_survives_global_warning_level(fresh_logging, monkeypatch):
    """团队日志是排查的核心，不能被全局 WARNING 压掉。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "WARNING")
    monkeypatch.setenv("HLZ_TEAM_LOG", "1")
    logging_setup.setup_logging()
    logging_setup.team_event("agent_idle", agent="beta")
    path = fresh_logging / f"team-{__import__('time').strftime('%Y%m%d')}.jsonl"
    assert path.exists() and path.read_text(encoding="utf-8").strip()


def test_setup_logging_is_idempotent(fresh_logging, monkeypatch):
    """重复调用不能装两遍 handler，否则每行日志会重复。"""
    monkeypatch.setenv("HLZ_TEAM_LOG", "1")
    logging_setup.setup_logging()
    n_first = len(logging.getLogger().handlers)
    logging_setup.setup_logging()
    assert len(logging.getLogger().handlers) == n_first


def test_team_event_does_not_duplicate_to_console(fresh_logging, monkeypatch, capfd):
    """团队事件只在 JSONL 里，不冒泡到 console（避免同一行两种格式）。"""
    monkeypatch.setenv("HLZ_LOG_LEVEL", "INFO")
    monkeypatch.setenv("HLZ_TEAM_LOG", "1")
    logging_setup.setup_logging()
    capfd.readouterr()
    logging_setup.team_event("only_in_file", agent="gamma")
    out = capfd.readouterr()
    assert "only_in_file" not in (out.out + out.err)
