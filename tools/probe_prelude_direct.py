"""探针 C：进程内直测 chat 前奏各阶段耗时（不经 HTTP）。

与探针 A/B 的分工：
  - A/B 走 HTTP，测「用户实际感知的」首帧时刻；
  - C 在进程内直调，把前奏拆成 A~F 六段，精确定位每一段的成本。

必须在**后端进程内**跑（需要 hub 已初始化）。独立进程直接跑会提示 hub 未初始化。
推荐用法：在后端所在的环境里挂到启动脚本，或用 uvicorn 的 --reload 加临时路由。

⚠️ 本探针需要 hub 已 build，因此主要价值是「设计阶段的量化依据」。
日常回归用 A/B 即可。

用法：
    python tools/probe_prelude_direct.py
    HLZ_UID=<user_id> python tools/probe_prelude_direct.py
"""
import asyncio
import os
import sys
import time

sys.path.insert(0, ".")

USER_ID = os.environ.get("HLZ_UID", "")


def ms(t0: float) -> float:
    return (time.monotonic() - t0) * 1000


async def main() -> int:
    if not USER_ID:
        print("需要 HLZ_UID")
        return 1

    from backend.app import db
    from backend.app.runtime import get_hub
    from backend.app.subagent.context import AgentContext
    from backend.app.subagent.session import build_turn_runtime, prepare_turn_team

    hub = get_hub()
    if hub is None:
        print("hub 未初始化：本探针需要在后端进程内运行（见文件头说明）")
        return 1

    sess = await db.create_session(USER_ID, title="probe-prelude-direct")
    sid = sess["id"]
    print(f"user={USER_ID} session={sid}")

    t0 = time.monotonic()
    ur = await hub.get_user_runtime(USER_ID, "")
    t_a1 = ms(t0)
    print(f"A1 get_user_runtime（冷，含 MCP 连接）: {t_a1:.1f} ms")

    await hub.get_user_runtime(USER_ID, "")
    t_a2 = ms(t0) - t_a1
    print(f"A2 get_user_runtime（热，缓存命中）    : {t_a2:.1f} ms")

    from backend.app.compaction import enforce_hard_limit, enforce_soft_limit

    t = time.monotonic()
    soft = await enforce_soft_limit(USER_ID, sid, pending_text="测试")
    print(f"B  enforce_soft_limit : {ms(t):8.1f} ms  -> {soft}")

    t = time.monotonic()
    hard = await enforce_hard_limit(USER_ID, sid)
    print(f"C  enforce_hard_limit : {ms(t):8.1f} ms  -> {hard}")

    t = time.monotonic()
    healed = await ur.sanitize_pending_tool_calls(sid)
    print(f"D  sanitize_pending   : {ms(t):8.1f} ms  -> {healed}")

    ctx = AgentContext(user_id=USER_ID, session_id=sid,
                       message_id="msg_probe", generation_id="g_probe")
    t = time.monotonic()
    turn = await build_turn_runtime(ur, ctx)
    t_e = ms(t)
    print(f"E  build_turn_runtime : {t_e:8.1f} ms")

    t = time.monotonic()
    await prepare_turn_team(turn, ctx.with_team(turn.team_id, role="lead"))
    print(f"E2 prepare_turn_team  : {ms(t):8.1f} ms")

    t = time.monotonic()
    inj = turn.loop.message_injector()
    if hasattr(inj, "__await__"):
        inj = await inj
    print(f"F  message_injector   : {ms(t):8.1f} ms  （注入 {len(inj or [])} 条）")

    print(f"\n首帧前合计（冷）: {t_a1 + t_e:.1f} ms")
    print(f"首帧前合计（热）: {t_a2 + t_e:.1f} ms")
    print(f"清理提示：可删除会话 {sid}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
