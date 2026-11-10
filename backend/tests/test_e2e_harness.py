"""e2e harness 的判据本身要被测（防止断言太宽）。"""
from __future__ import annotations


def _event(tid, status):
    return {"type": "task_updated", "payload": {"task": {"task_id": tid, "status": status}}}


def test_evaluate_flags_incomplete_tasks():
    from tests.e2e.harness import evaluate, summarize

    events = [_event("t1", "completed"), _event("t2", "in_progress")]
    summary = summarize(events)
    checks = evaluate({"expect_all_tasks_completed": True, "min_tasks": 2}, summary)
    assert checks[0]["ok"] is False


def test_evaluate_passes_when_all_completed():
    from tests.e2e.harness import evaluate, summarize

    events = [_event("t1", "completed"), _event("t2", "completed")]
    summary = summarize(events)
    checks = evaluate({"expect_all_tasks_completed": True, "min_tasks": 2}, summary)
    assert checks[0]["ok"] is True


def test_evaluate_fails_when_too_few_tasks():
    from tests.e2e.harness import evaluate, summarize

    summary = summarize([_event("t1", "completed")])
    checks = evaluate({"expect_all_tasks_completed": True, "min_tasks": 5}, summary)
    assert checks[0]["ok"] is False


def test_summarize_keeps_last_status_per_task():
    from tests.e2e.harness import summarize

    events = [_event("t1", "pending"), _event("t1", "completed")]
    assert summarize(events)["tasks"] == {"t1": "completed"}


def test_expect_events_checks_event_types():
    from tests.e2e.harness import evaluate, summarize

    events = [{"type": "subagent_status", "status": "stopping"},
              {"type": "subagent_end", "status": "stopped"}]
    summary = summarize(events)
    ok = evaluate({"expect_events": ["subagent_status"]}, summary)
    bad = evaluate({"expect_events": ["team_start"]}, summary)
    assert ok[0]["ok"] is True
    assert bad[0]["ok"] is False


def test_summarize_exposes_event_types():
    from tests.e2e.harness import summarize

    summary = summarize([{"type": "subagent_status"}, {"type": "text_delta", "text": "hi"}])
    assert "subagent_status" in summary["event_types"]


def test_tool_result_sentinel_checks_tool_output_not_reply():
    from tests.e2e.harness import evaluate, summarize

    events = [{"type": "tool_result", "name": "code_run", "content": "[12345 chars omitted; full output: x.txt]"},
              {"type": "text_delta", "text": "跑完了"}]
    summary = summarize(events)
    ok = evaluate({"tool_result_contains": "chars omitted"}, summary)
    bad = evaluate({"tool_result_contains": "no such marker"}, summary)
    assert ok[0]["ok"] is True
    assert bad[0]["ok"] is False
