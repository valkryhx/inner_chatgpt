"""核验排查经验文档里的每条命令/查询是否真能跑、结论是否与当时一致。

## 为什么要这条

这份文档是我"事后总结"的，最容易出的错是：
**把当时用的命令记错**（表名写错、字段名写错、参数写错）。

那种错误比结论错更坏 —— 下次照着抄会失败，然后人会怀疑整个方法。

## 核验范围

1. 文档 §2 的三条 SQL 是否能跑、字段名是否真实存在
2. 文档 §3 提到的日志窗口命令格式是否正确
3. 文档 §4 的 find 命令是否能跑
4. 文档引用的会话 ID 与结论是否与 DB 实际一致
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

PG = "hlzsbxprobe-postgres-1"
SID = "co_8abfb27422c8"


def psql(sql: str) -> tuple[int, str]:
    """在容器里跑一条 SQL，返回 (rc, 输出)。

    ⚠️ 必须 `MSYS_NO_PATHCONV=1`：Git Bash 会把 `/d ...` 之类的参数
    当成路径改写，`-c` 里的 SQL 含 `/` 时会被破坏（实测过）。
    """
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "hlz", "-d", "hlaudz", "-tAc", sql],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def sh(cmd: str, container: str = "hlzsbxprobe-app-1") -> tuple[int, str]:
    env = {**os.environ, "MSYS_NO_PATHCONV": "1"}
    proc = subprocess.run(
        ["docker", "exec", container, "sh", "-c", cmd],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", env=env,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


# (名称, SQL, 期望在输出里出现的内容)
SQL_CHECKS = [
    (
        "§2 子代理状态查询",
        f"""SELECT name, status, turns FROM subagent_runs
            WHERE session_id='{SID}' ORDER BY started_at""",
        "researcher_a",
    ),
    (
        "§2 任务状态查询",
        f"""SELECT status, owner_agent_id FROM team_tasks
            WHERE session_id='{SID}'""",
        "completed",
    ),
    (
        "§2 依赖查询",
        """SELECT task_id, blocked_by FROM team_task_deps LIMIT 1""",
        "task_",
    ),
]

# (名称, shell 命令, 期望能跑通)
SH_CHECKS = [
    ("§4.2 权限逐级核对",
     "find /app/backend/data/workspaces -maxdepth 3 -type d -exec ls -ld {} \\; | head -3"),
    ("§4.2 属主异常查找",
     "find /app/backend/data/workspaces -maxdepth 4 \\( -uid 65534 -o -gid 65534 \\) | head -3"),
]


# 文档 §2.1 的终态结论：4 个 writer（+ 1 个 researcher = 5 条 run）、撰写任务 completed（owner=sub:writer_e）
#
# ⚠️ 这条断言本身也修过一次：第一版断言「有 pending 任务」，
# 那是把**排查中途的快照**当成了终态。回查终态发现任务最终 completed，
# 但中间换了 5 个 writer。文档已按终态改写。
FINAL_EXPECT = {
    "writer_count": 4,
    "writer_e_status": "stopped",
    "report_owner": "sub:writer_e",
}


def main() -> int:
    problems: list[str] = []
    ok = 0

    for name, sql, expect in SQL_CHECKS:
        rc, out = psql(sql)
        if rc != 0:
            problems.append(f"{name}: SQL 失败 rc={rc}\n{out[:400]}")
            print(f"[✗] {name}: SQL 跑不通")
            continue
        if expect not in out:
            problems.append(f"{name}: 输出里没有 {expect!r}\n{out[:300]}")
            print(f"[✗] {name}: 结果与文档不符")
            continue
        print(f"[✓] {name}")
        ok += 1

    for name, cmd in SH_CHECKS:
        rc, out = sh(cmd)
        if rc != 0:
            problems.append(f"{name}: 命令失败 rc={rc}\n{out[:300]}")
            print(f"[✗] {name}: 命令跑不通")
            continue
        print(f"[✓] {name}（输出 {len(out.splitlines())} 行）")
        ok += 1

    # ---- §2.1 终态断言 ----
    rc, out = psql(
        f"SELECT count(*) FROM subagent_runs WHERE session_id='{SID}' AND name LIKE 'writer%'")
    n = out.strip()
    if rc == 0 and n == str(FINAL_EXPECT["writer_count"]):
        print(f"[✓] §2.1 终态：{n} 个 writer（与文档一致）")
        ok += 1
    else:
        problems.append(
            f"§2.1 终态 writer 数量：DB={n!r}，文档=4。"
            "若这个会话又起了新的 writer，说明文档的『终态』说法已经不成立，"
            "需要重新取样并更新文档")
        print(f"[✗] §2.1 终态：DB={n!r} 文档=4")

    rc, out = psql(
        f"""SELECT owner_agent_id FROM team_tasks WHERE session_id='{SID}'
            AND subject LIKE '撰写%'""")
    owner = out.strip().splitlines()[-1] if out.strip() else ""
    if rc == 0 and owner == FINAL_EXPECT["report_owner"]:
        print(f"[✓] §2.1 终态：撰写任务 owner={owner}（与文档一致）")
        ok += 1
    else:
        problems.append(
            f"§2.1 终态 owner：DB={owner!r}，文档=sub:writer_e —— 需重新取样")
        print(f"[✗] §2.1 终态 owner：DB={owner!r} 文档=sub:writer_e")

    # ---- §3.1 的教训：日志必须带 --since ----
    proc = subprocess.run(
        ["docker", "logs", "--since", "1h", "hlzsbxprobe-sandbox-1"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace",
        env={**os.environ, "MSYS_NO_PATHCONV": "1"},
    )
    if proc.returncode == 0:
        print("[✓] §3.1 docker logs --since 可用")
        ok += 1
    else:
        problems.append(f"docker logs --since 失败：{proc.stderr[:200]}")
        print("[✗] §3.1 docker logs --since 不可用")

    print()
    if problems:
        print(f"✗ 失败 {len(problems)} 项：")
        for p in problems:
            print("  -", p)
        return 1
    print(f"✓ 全部通过（{ok} 项）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())