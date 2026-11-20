"""核验 inner_chatgpt/AGENTS.md 新增章节里的每条命令能不能真跑。

## 为什么要这条

AGENTS.md 是**每个新 agent 开工前必读**的文件。里面的 SQL 表名/字段名
写错，会让每个后来者照着抄然后失败 —— 而失败者会先怀疑自己，再怀疑整个方法。

比文档结论错更坏，因为它是入口。

## 核验范围

1. §3.1 的三条 SQL（表名 + 字段名 + 语法）
2. §3.3 的 docker logs --since 形式
3. 章节编号引用是否自洽（§2.6 指向的 §4 真的存在）
4. 必读清单里新加的那条路径真的存在
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess
import sys

def _find_repo_root(start: pathlib.Path) -> pathlib.Path:
    """向上找含 `backend/app/workspace.py` 的那一层。

    ⚠️ 不能写死 `parents[1]`：本文件在 `docs/probes/` 下，
    `parents[1]` 是 `docs/` 而不是仓库根 —— 实测报「找不到 AGENTS.md」。
    """
    for p in [start, *start.parents]:
        if (p / "backend" / "app" / "workspace.py").is_file():
            return p
    raise SystemExit(f"找不到仓库根（从 {start} 向上）")


REPO = _find_repo_root(pathlib.Path(__file__).resolve().parent)
AGENTS = REPO / "AGENTS.md"
PG = "hlzsbxprobe-postgres-1"
SID = "co_8abfb27422c8"

ENV = {**os.environ, "MSYS_NO_PATHCONV": "1", "PYTHONIOENCODING": "utf-8"}


def psql(sql: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["docker", "exec", PG, "psql", "-U", "hlz", "-d", "hlaudz", "-tAc", sql],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", env=ENV,
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    problems: list[str] = []
    ok = 0

    if not AGENTS.is_file():
        print("找不到 AGENTS.md")
        return 1
    text = AGENTS.read_text(encoding="utf-8")

    # ---- 1) 三条 SQL：字段名必须真实存在 ----
    sqls = {
        "§3.1 子代理查询": f"""SELECT name, status, turns,
            (finished_at-started_at)/1000 AS secs
            FROM subagent_runs WHERE session_id='{SID}' ORDER BY started_at""",
        "§3.1 任务查询": f"""SELECT substr(subject,1,20), status, owner_agent_id
            FROM team_tasks WHERE session_id='{SID}'""",
        "§3.1 依赖查询": """SELECT task_id, blocked_by FROM team_task_deps LIMIT 1""",
    }
    for name, sql in sqls.items():
        rc, out = psql(sql)
        if rc != 0:
            problems.append(f"{name}: SQL 跑不通\n{out[:300]}")
            print(f"[✗] {name}")
        else:
            print(f"[✓] {name}（{len(out.splitlines())} 行）")
            ok += 1

    # ---- 2) 文档里的 SQL 文本必须能跑（不只是我改写过的那版）----
    # 从 AGENTS.md 里抽出 SELECT 语句原样跑。
    #
    # ⚠️ 第一版用 `raw.strip().startswith("select")` 判断，结果**三条全被跳过**：
    # 文档里的 SQL 每段前面都有 `-- 子问题：…` 注释行，strip 后首字符是 `-`。
    # 而且它不报错，静默跳过 —— 看着像「都通过了」，实际一条没验。
    # 修法：先剥掉注释行和空行，再判断。
    doc_sql_count = 0
    for m in re.finditer(r"```sql\n(.*?)```", text, re.S):
        block = m.group(1)
        for raw in block.split(";"):
            lines = [ln for ln in raw.splitlines()
                     if ln.strip() and not ln.strip().startswith("--")]
            sql = "\n".join(lines).strip()
            if not sql or not re.match(r"(?i)^select\b", sql):
                continue
            doc_sql_count += 1
            probe = sql.replace("<sid>", SID).replace("<tid>", "team_657776bfb940")
            rc, out = psql(probe)
            first = " ".join(probe.split())[:60]
            if rc != 0:
                problems.append(f"AGENTS.md 里的 SQL 跑不通：{first}…\n{out[:300]}")
                print(f"[✗] 文档 SQL: {first}…")
            else:
                print(f"[✓] 文档 SQL: {first}…")
                ok += 1
    # 文档里确实有 sql 代码块却一条都没验到 => 脚本自己有 bug，必须报错
    if "```sql" in text and doc_sql_count == 0:
        problems.append("AGENTS.md 含 ```sql 代码块，但一条 SELECT 都没被抽出来 —— 抽取逻辑失效")
        print("[✗] 抽取逻辑失效：有 sql 块但零条被验")

    # ---- 3) docker logs --since 形式可用 ----
    proc = subprocess.run(
        ["docker", "logs", PG, "--since", "1h"],
        capture_output=True, text=True, timeout=60,
        encoding="utf-8", errors="replace", env=ENV,
    )
    if proc.returncode == 0:
        print("[✓] docker logs --since 可用")
        ok += 1
    else:
        problems.append(f"docker logs --since 失败：{proc.stderr[:200]}")
        print("[✗] docker logs --since 不可用")

    # ---- 4) 章节引用自洽：§2.6 指向的 §4 存在 ----
    if "与 §4 TDD 的衔接" in text:
        if re.search(r"^## 4\. ", text, re.M):
            print("[✓] §2.6 → §4 引用自洽")
            ok += 1
        else:
            problems.append("§2.6 引用 §4，但文档里没有「## 4.」章节")
            print("[✗] §2.6 → §4 引用悬空")
    else:
        problems.append("找不到 §2.6 的标题")
        print("[✗] §2.6 标题缺失")

    # 全文不应再有 §8.5 悬空引用
    n = text.count("§8.5")
    if n:
        problems.append(f"仍有 {n} 处 §8.5 悬空引用（本文件没有该章节）")
        print(f"[✗] 残留 {n} 处 §8.5")
    else:
        print("[✓] 无 §8.5 悬空引用")
        ok += 1

    # ---- 5) 必读清单里的新路径真的存在 ----
    key = "docs/20261120-排查经验-从卡住到根因的四路交叉.md"
    in_list = key in text[: text.find("### 0.3 目录约定")]
    exists = (REPO / key).is_file()
    if in_list and exists:
        print("[✓] 必读清单条目存在且文件存在")
        ok += 1
    else:
        problems.append(f"必读清单条目 in_list={in_list} file_exists={exists}")
        print(f"[✗] 必读清单：in_list={in_list} exists={exists}")

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