import subprocess, sys, pathlib

def case(path, label, old, new, tests):
    p = pathlib.Path(path)
    orig = p.read_text(encoding="utf-8")
    if old not in orig:
        print(f"[SKIP] {label}"); return
    p.write_text(orig.replace(old, new, 1), encoding="utf-8", newline="\n")
    r = subprocess.run([sys.executable, "-m", "pytest", tests, "-q"],
                       cwd="backend", capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    tail = [l for l in (r.stdout or "").splitlines() if "passed" in l or "failed" in l]
    print(f"[{'RED(正确)' if r.returncode else 'GREEN(漏了!)'}] {label} -> {tail[-1] if tail else r.returncode}")
    p.write_text(orig, encoding="utf-8", newline="\n")

case("backend/app/memory_extract.py", "钩子改回同步 await（阻塞 SSE）",
     "        task = asyncio.create_task(_run_detached(user_id, client, snapshot))\n        _inflight[user_id] = task\n        _background.add(task)\n        task.add_done_callback(_background.discard)",
     "        await _run_detached(user_id, client, snapshot)",
     "tests/test_memory_extract.py -k does_not_block")

case("backend/app/memory_extract.py", "去掉每用户并发限制",
     "        if _inflight.get(user_id):",
     "        if False:",
     "tests/test_memory_extract.py -k limits_concurrency")

case("backend/app/memory_extract.py", "不传 history 快照（直接传引用）",
     "        snapshot = [dict(m) for m in history if isinstance(m, dict)]",
     "        snapshot = history",
     "tests/test_memory_extract.py -k snapshots_history")

case("backend/app/memory_extract.py", "提取器给全套 root 工具（不闭世界）",
     '    tools = [t for t in build_memory_tools(user_id) if t.name == "memory_write"]',
     '    tools = build_memory_tools(user_id)',
     "tests/test_memory_extract.py -k registry")

case("backend/app/memory_extract.py", "关掉时也消耗轮次计数",
     "    if ms.memory_hard_disabled() or not ms.auto_write_enabled():\n        decision = mw.evaluate_gates([], turn_count=0)\n        return ExtractOutcome(ran=False, reason=decision.reason)",
     "    if ms.memory_hard_disabled() or not ms.auto_write_enabled():\n        _turn_counter(user_id).tick()\n        decision = mw.evaluate_gates([], turn_count=0)\n        return ExtractOutcome(ran=False, reason=decision.reason)",
     "tests/test_memory_extract.py -k does_not_tick")
print("已还原")
