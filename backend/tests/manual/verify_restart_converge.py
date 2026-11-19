"""在真实运行的服务上验证 #1：重启不会杀掉别的实例正在跑的会话。

## 这个探针与 probe_initdb_cross_instance_wipe.py 的区别

那个是**单元级**的：同一个进程里连跑两次 `converge_stale_streaming`。
这个走**真实 HTTP + 真实重启**，验证「进程真的重启了，库里的活会话还在」。

两者的差别正是审计文档里写的那条局限（「没有做真容器两副本的端到端验证」）。
这个探针把「真实重启」这一环补上 —— 虽然仍是单机，但重启是真的。

## 做法

1. 直接在真实库里造一条 streaming 消息 + 一条 owner 是**别的实例**且存活的
   `session_generations`（存活的判定靠 `cluster.instances.live_owners()`，
   本机没配 Redis 时那个集合为空 —— 所以 owner 得是「空串」以外、
   且在存活集合里的值才叫「活着」。本机无法满足，所以改用另一种做法）。

本机没配 Redis，`live_owners()` 恒空 => 收敛必然全清。这是**正确的单实例语义**，
所以这个探针在单机上只能验证「单实例行为没变」，不能验证「多实例不误伤」。

真正验证多实例不误伤，靠的是 `tests/test_startup_converge_scope.py` 里
`test_other_instance_generation_is_kept`（注入存活集合）。

所以本探针**不造活实例**，只验证两件事：
- 单机上重启确实会收敛残留（行为没变）
- 收敛**不碰**非 streaming 的消息（不要扩大打击面）
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
DB = BACKEND / "data" / "app.db"


def main() -> int:
    if not DB.exists():
        print(f"找不到真实库 {DB}")
        return 2

    c = sqlite3.connect(DB)
    uid = "u_restart_probe"
    sid = "s_restart_probe"
    ms_stream = "m_restart_streaming"
    ms_done = "m_restart_done"

    def cleanup() -> None:
        c.execute("DELETE FROM messages WHERE id IN (?,?)", (ms_stream, ms_done))
        c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        c.execute("DELETE FROM users WHERE id=?", (uid,))
        c.commit()

    cleanup()

    c.execute(
        "INSERT INTO users(id,email,name,username,password_hash,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (uid, "restart-probe@test.invalid", "rp", "restart_probe", "x", 1),
    )
    c.execute(
        "INSERT INTO sessions(id,user_id,title,created_at,updated_at) VALUES(?,?,?,?,?)",
        (sid, uid, "rp", 1, 1),
    )
    c.execute(
        "INSERT INTO messages(id,session_id,role,content,created_at,status) "
        "VALUES(?,?,?,?,?,?)",
        (ms_stream, sid, "assistant", "", 1, "streaming"),
    )
    c.execute(
        "INSERT INTO messages(id,session_id,role,content,created_at,status) "
        "VALUES(?,?,?,?,?,?)",
        (ms_done, sid, "assistant", "已经完成了", 1, "complete"),
    )
    c.commit()

    print(f"已造数据：{ms_stream}=streaming, {ms_done}=complete")
    print()
    print("现在请重启服务（或由调用方重启），然后跑：")
    print("    python tests/manual/verify_restart_converge.py --after")
    print()
    print(f"清理命令：sqlite3 里 DELETE from messages where id in ('{ms_stream}','{ms_done}')")
    return 0


def check_after() -> int:
    c = sqlite3.connect(DB)
    rows = dict(c.execute(
        "SELECT id, status FROM messages WHERE id IN (?,?)",
        ("m_restart_streaming", "m_restart_done")).fetchall())
    if not rows:
        print("找不到探针数据 —— 先跑一次不带参数的本脚本")
        return 2

    streaming_after = rows.get("m_restart_streaming")
    done_after = rows.get("m_restart_done")
    print(f"streaming 消息重启后 = {streaming_after!r}（单实例下期望 'stopped'）")
    print(f"complete  消息重启后 = {done_after!r}（期望仍是 'complete'）")

    ok = streaming_after == "stopped" and done_after == "complete"
    print()
    if ok:
        print("PASS：单实例行为没变（残留被收敛，已完成的没被动）")
    else:
        print("FAIL：收敛行为与预期不符")
    return 0 if ok else 1


if __name__ == "__main__":
    if "--after" in sys.argv:
        code = check_after()
        if code == 0:
            # 顺手清理
            c = sqlite3.connect(DB)
            c.execute("DELETE FROM messages WHERE id IN (?,?)",
                      ("m_restart_streaming", "m_restart_done"))
            c.execute("DELETE FROM sessions WHERE id='s_restart_probe'")
            c.execute("DELETE FROM users WHERE id='u_restart_probe'")
            c.commit()
            print("（已清理探针数据）")
        sys.exit(code)
    sys.exit(main())