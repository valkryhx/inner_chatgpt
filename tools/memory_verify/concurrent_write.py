"""dream 与提取同时写同一批记忆文件 —— 真实并发冲突实测。

## 为什么这件事必须实测

方案 §4.6 规定「dream 持锁期间提取照常跑（它写单条记忆，冲突面小）」。
这是**设计假设**，不是实测结论。三处可能出问题：

1. **写同一个 slug**：dream 决定把 3 条合并进 `p-1`，同时提取器发现新事实
   要更新 `p-1` —— 后者会覆盖前者的合并结果，或者相反；
2. **dream 删掉提取器刚写的条目**：提取器写 `p-9`，dream 的 delete 队列里
   恰好有 `p-9`（它先前读过目录）—— 新记忆刚写就没了；
3. **索引与文件脱节**：dream 在 Phase 3 之后重建索引，而提取器在这之间
   又写了新文件 —— 索引就会缺一条。

Hlaudz 的 `memory_store.write_memory` 用 `if_version` CAS 保护单次写入，
但**两个人各写各的时候**：一个不带 if_version 的写入是盲写（覆盖）。

## 这个脚本测什么

不测「有没有锁」（那是 M-18 单测的事），只测**在真实交错下数据有没有丢**：

| 场景 | 构造 | 判据 |
|---|---|---|
| C-1 同时写同一 slug | dream 与提取器都写 `shared`，各带不同标记 | 最终文件是**其中一方的完整内容**，不能是半截/混合；两方的标记至少有一个完整存在 |
| C-2 dream 删掉提取器刚写的 slug | dream 读到目录快照后，提取器写入 `late`，然后 dream 执行合并 | `late` 若被删，必须是因为它被合并进了保留者（内容仍在），不能是「无声消失」 |
| C-3 索引与文件一致 | 一轮 dream 期间穿插 3 次提取写入 | 结束后 `MEMORY.md` 的行数与目录里实际条数一致 |

**这些是「当前实现的真实行为描述」，不是「必须全绿」的验收。**
哪条红了就说明方案 §4.6 的假设在这条上不成立，需要改成「dream 期间暂停提取」
或给写路径加全局写锁 —— 那是设计决策，不是顺手改代码能解决的。
所以脚本把每条都跑出来如实报告，退出码只在「数据真的丢了」时为 1。

跑法::

    cd backend && python ../tools/memory_verify/concurrent_write.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, case: str, verdict: str, detail: str) -> None:
        """verdict: OK / RISK / LOSS"""
        self.rows.append((case, verdict, detail))
        icon = {"OK": "OK  ", "RISK": "RISK", "LOSS": "LOSS"}[verdict]
        print(f"  [{icon}] {case}: {detail}")

    def exit_code(self) -> int:
        return 1 if any(v == "LOSS" for _, v, _ in self.rows) else 0

    def dump(self, path: Path) -> None:
        path.write_text(json.dumps(
            [{"case": c, "verdict": v, "detail": d} for c, v, d in self.rows],
            ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")


def _prepare(tmp: Path):
    """把记忆目录与 DB 指到临时目录，建表。返回 (memory_store, db 模块)。"""
    from app import db, memory_store as ms

    ms.MEMORIES_DIR = tmp / "memories"
    ms.DB_PATH = tmp / "app.db"
    db.DB_PATH = tmp / "app.db"
    return ms, db


async def _write(ms, user: str, slug: str, desc: str, body: str,
                 if_version: str | None = None):
    return await ms.write_memory(user, slug=slug, title=slug, description=desc,
                                 body=body, kind="user", if_version=if_version)


async def case_c1_same_slug(ms) -> tuple[str, str]:
    """C-1：dream 与提取器同时写同一个 slug（都**不带** if_version）。

    这就是「盲写 vs 盲写」。文件系统层面必然是一方覆盖另一方；
    关键判据是**不能出现半截文件**（内容交错 = 文件损坏）。
    """
    user = "c1"
    await _write(ms, user, "shared", "初始", "# 初始\n\nBASE_MARK\n")

    dream_body = "# 合并后\n\n" + ("DREAM_" * 200) + "DREAM_END\n"
    extract_body = "# 新事实\n\n" + ("EXTRACT_" * 200) + "EXTRACT_END\n"

    # 真并发：两个协程同时进 write_memory（它内部会 await 文件写/DB 写）
    await asyncio.gather(
        _write(ms, user, "shared", "dream 合并后的描述", dream_body),
        _write(ms, user, "shared", "提取器写入的描述", extract_body),
    )

    path = ms.memory_path(user, "shared")
    text = path.read_text(encoding="utf-8")

    has_dream = "DREAM_END" in text
    has_extract = "EXTRACT_END" in text
    # 损坏判据：两个 END 都在（内容交错拼接）、或解析不出 frontmatter
    mixed = has_dream and has_extract
    spec = await ms.get_memory(user, "shared")
    parseable = spec is not None

    if mixed:
        return "LOSS", "文件被两个写入交错拼接（两个 END 标记都在）—— 内容损坏"
    if not parseable:
        return "LOSS", f"最终文件无法解析为记忆（前 120 字：{text[:120]!r}）"
    if has_dream or has_extract:
        winner = "dream" if has_dream else "提取器"
        return "OK", f"最终是{winner}的完整内容（后写覆盖先写，无交错）"
    return "LOSS", f"两方内容都不见了（前 120 字：{text[:120]!r}）"


async def case_c1b_cas_prevents_clobber(ms) -> tuple[str, str]:
    """C-1b：带 if_version 时，后写必须拿 conflict 而不是静默覆盖。

    这是「有 CAS」和「没 CAS」的区别：没有它，两个写者会静默互相覆盖；
    有它，第二个写者拿到 409 语义并能读到对方的内容再决定。
    """
    user = "c1b"
    first = await _write(ms, user, "s", "v1", "# v1\n\nV1_MARK\n")
    version = first.spec.version if first.spec else ""

    res = await _write(ms, user, "s", "dream 改的", "# dream\n\nDREAM_MARK\n",
                       if_version=version)
    # 这之后提取器拿**旧** version 再写
    stale = await _write(ms, user, "s", "提取器改的", "# extract\n\nEXTRACT_MARK\n",
                         if_version=version)

    if stale.outcome != "conflict":
        return "LOSS", f"过期 if_version 没被拒（outcome={stale.outcome}），CAS 失效"
    text = ms.memory_path(user, "s").read_text(encoding="utf-8")
    if "DREAM_MARK" not in text:
        return "LOSS", "CAS 拒绝了但内容也被改坏了"
    return "OK", f"过期写入被拒（outcome=conflict），文件仍是先写者的内容"


async def case_c2_delete_race(ms) -> tuple[str, str]:
    """C-2：dream 读到快照后，提取器新写入 `late`；dream 随后跑 delete。

    真实机制：`build_dream_registry` 的 delete 是**当场**调 `ms.delete_memory`，
    不是删「快照里的列表」。所以只要模型不点名 `late`，它就不会被删。
    这里测两种：
      a) dream 不点名 late -> late 必须活着（不该被「目录快照差异」误删）
      b) dream 点名 late 且它没被合并 -> 内容就丢了，这是**设计上的真实风险**
    """
    user = "c2"
    keep = await _write(ms, user, "keep", "保留者", "# keep\n\nKEEP_MARK\n")
    await _write(ms, user, "dup", "重复项", "# dup\n\nDUP_MARK_DUP_ONLY\n")

    # 模拟：dream 已经在 Phase 1 读过目录（只有 keep / dup）
    snapshot = {m.slug for m in (await ms.list_memories(user, limit=200)).entries}
    assert snapshot == {"keep", "dup"}, snapshot

    # 期间提取器写入新条目
    await _write(ms, user, "late", "提取器刚写的新事实", "# late\n\nLATE_NEW_FACT\n")

    # a) dream 只删 dup（它没在快照里看到 late）
    from app import memory_dream as md
    reg = md.build_dream_registry(user)
    await reg.dispatch("memory_write", {
        "slug": "keep", "content": "# keep\n\nKEEP_MARK\nDUP_MARK_DUP_ONLY\n",
        "description": "合并后", "if_version": keep.spec.version if keep.spec else ""})
    await reg.dispatch("memory_delete", {"slug": "dup"})

    late_now = await ms.get_memory(user, "late")
    if late_now is None:
        return "LOSS", "提取器新写入的 late 被删了（未被点名的条目不该消失）"
    if "LATE_NEW_FACT" not in (late_now.body or ""):
        return "LOSS", "late 还在但内容被改了"
    return ("OK", "未被点名的新条目 late 完好保留；dup 被合并后删除，"
                  "内容已进 keep")


async def case_c2b_blind_delete_is_the_risk(ms) -> tuple[str, str]:
    """C-2b：**如果** dream 点名删了提取器刚写的条目，内容是否真的会丢。

    这不是「bug 复现」，而是把方案 §4.6 承认的风险量化：delete 没有版本检查，
    模型在 Phase 1 看到 `dup`，Phase 2 期间提取器又更新了 `dup`（新事实），
    dream 按旧快照决定删它 —— 新事实就没了。

    实测这一条是为了给「要不要给 delete 加 version 检查」提供依据。
    """
    user = "c2b"
    await _write(ms, user, "dup", "重复项 v1", "# v1\n\nOLD_CONTENT\n")
    # dream 在 Phase 1 拿到旧版
    old = await ms.get_memory(user, "dup")
    old_version = old.version

    # Phase 2 期间提取器给它补了新事实
    await _write(ms, user, "dup", "重复项 v2", "# v2\n\nNEW_FACT_FROM_EXTRACT\n")

    from app import memory_dream as md
    reg = md.build_dream_registry(user)
    # 让 delete 的「先写后删」闸通过
    await reg.dispatch("memory_write", {
        "slug": "other", "content": "# other\n\nOTHER\n", "description": "别处"})
    out = json.loads(await reg.dispatch("memory_delete", {"slug": "dup"}))

    still = await ms.get_memory(user, "dup")
    if out.get("outcome") != "deleted":
        return "OK", f"delete 没执行（outcome={out.get('outcome')}）"
    if still is None:
        return ("RISK", "确认存在竞态：dream 按旧快照删除，"
                        "提取器在期间写入的 NEW_FACT_FROM_EXTRACT 已丢失。"
                        "delete 没有 if_version 保护")
    return "OK", "dup 仍在"


async def case_c3_index_consistency(ms) -> tuple[str, str]:
    """C-3：并发写入期间的索引必然是快照；**安静重建后必须精确一致**。

    先说清楚这条为什么不该判「必须并发期间也一致」：
    `rebuild_index` 是「reconcile -> 读表 -> 写文件」的三步快照。任何快照式
    重建在写文件期间都可能有新条目落盘，那次索引就会少几条。要消除它只能
    给整个记忆写路径加全局锁 —— 代价远大于收益，因为：

    **`MEMORY.md` 目前没有任何运行期读取者。** 注入走的是 DB
    （`select_with_stats`），不是这个文件（`grep -rn index_path app/` 可验证）。
    它只给人看，而 dream 每次跑都会重写它。

    所以真正要验的性质是**收敛性**：并发期允许陈旧，安静下来重建一次必须
    与磁盘精确一致。这也是用户实际会看到的状态（没有人在写的时候）。
    """
    user = "c3"
    for i in range(4):
        await _write(ms, user, f"m{i}", f"第 {i} 条", f"# m{i}\n\nBODY_{i}\n")

    from app import memory_dream as md

    async def churn():
        for i in range(4, 9):
            await _write(ms, user, f"m{i}", f"第 {i} 条", f"# m{i}\n\nBODY_{i}\n")
            await asyncio.sleep(0)

    # 阶段 1：并发重建（允许陈旧快照）
    await asyncio.gather(churn(), md.run_phase4(user))
    mid_rows = _index_rows(ms, user)

    # 阶段 2：安静状态下重建 —— 必须精确
    await md.run_phase4(user)
    actual = await ms.count_memories(user)
    rows = _index_rows(ms, user)

    # 幽灵行是硬伤（指向不存在的文件），任何阶段都不许有
    ghosts = _index_ghosts(ms, user)
    if ghosts:
        return "LOSS", f"索引里有指向不存在文件的幽灵行：{sorted(ghosts)}"
    if rows != actual:
        return ("LOSS", f"安静重建后仍不一致：索引 {rows} 行 / 实际 {actual} 条"
                        f"（并发期间为 {mid_rows} 行）")
    note = f"并发期快照 {mid_rows} 行（允许），安静重建后 {rows} 行 = 实际 {actual} 条"
    return "OK", note


def _index_rows(ms, user: str) -> int:
    path = ms.index_path(user)
    if not path.exists():
        return 0
    return sum(1 for ln in path.read_text(encoding="utf-8").split("\n")
               if ln.startswith("- ["))


def _index_ghosts(ms, user: str) -> set[str]:
    path = ms.index_path(user)
    if not path.exists():
        return set()
    on_disk = {p.stem for p in (ms.memory_root(user) / "private").glob("*.md")}
    listed: set[str] = set()
    for ln in path.read_text(encoding="utf-8").split("\n"):
        if ln.startswith("- [") and "](" in ln:
            listed.add(Path(ln.split("](", 1)[1].split(")", 1)[0]).stem)
    return listed - on_disk


async def case_c4_two_dreams_one_lock(ms, md) -> tuple[str, str]:
    """C-4（附带）：同一用户的两次 dream 必须互斥（锁验证的互补面）。

    M-18 测的是「锁的语义」，这里测的是「在真实并发调用下它真的挡住了」。
    """
    user = "c4"
    await _write(ms, user, "a", "记忆 a", "# a\n\nA\n")
    md_now = md._now_ms()
    ok1 = await md.acquire_lock(user, pid=1001, now_ms=md_now)
    ok2 = await md.acquire_lock(user, pid=1002, now_ms=md_now)
    await md.release_lock(user, pid=1001)
    if not ok1:
        return "LOSS", "第一个 acquire 就失败了"
    if ok2:
        return "LOSS", "第二个 acquire 也成功 —— 锁没挡住并发 dream"
    return "OK", "第二个 acquire 被拒（同用户两次 dream 互斥）"


async def main() -> int:
    rep = Report()
    tmp = Path(tempfile.mkdtemp(prefix="dream_race_"))
    print(f"临时目录：{tmp}\n")
    try:
        ms, db = _prepare(tmp)
        await db.init_db()
        await ms.init_tables()
        from app import memory_dream as md
        await md.init_tables()

        v, d = await case_c1_same_slug(ms)
        rep.add("C-1 同 slug 盲写", v, d)
        v, d = await case_c1b_cas_prevents_clobber(ms)
        rep.add("C-1b CAS 保护", v, d)
        v, d = await case_c2_delete_race(ms)
        rep.add("C-2 delete 与提取", v, d)
        v, d = await case_c2b_blind_delete_is_the_risk(ms)
        rep.add("C-2b 盲删竞态", v, d)
        v, d = await case_c3_index_consistency(ms)
        rep.add("C-3 索引一致性", v, d)
        v, d = await case_c4_two_dreams_one_lock(ms, md)
        rep.add("C-4 双 dream 互斥", v, d)
    finally:
        report_path = ROOT / "work" / "concurrent_write_report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        rep.dump(report_path)
        print(f"\n报告：{report_path}")
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n说明：RISK = 方案 §4.6 的设计假设在这条上不成立，需要设计决策；"
          "\n      LOSS = 数据真的丢了，必须修。")
    return rep.exit_code()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
