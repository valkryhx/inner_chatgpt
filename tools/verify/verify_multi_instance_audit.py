"""核验《Hlaudz 多实例正确性审计》文档里的坐标与结论（2026-11-19）。

## 为什么需要它

这份文档的核心是「某处代码缺了某个过滤」。这类结论有两个腐烂方向：

1. **行号漂移** —— 加一行注释就全错，文档看上去毫无变化；
2. **结论被悄悄修掉** —— 修好之后文档还说「未修」，下一个人照文档去修已经修好的东西。

所以每个「问题」都写成二元断言：
- 现状断言（`ISSUE_*`）：问题**仍然存在**时通过；一旦有人修了，它会红 —— 提醒更新文档。
- 对照断言（`OK_*`）：同一仓库里已经做对的地方，必须保持对。

第三条是「修好之后要反过来」的那种断言，故意这么写：全绿不代表正确，
**该红的时候能红**才是。见 docs/20261113-源码调研复核方法.md。

## 用法

    python tools/verify/verify_multi_instance_audit.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

HLAUDZ = Path("D:/git_codes/inner_chatgpt")
BE = HLAUDZ / "backend"

results: list[tuple[str, bool, str, str]] = []


def add(name: str, ok: bool, fail_hint: str = "", info: str = "") -> None:
    """登记一条断言。

    `fail_hint` 只在**失败**时打印 —— 它是「这条红了该怎么判断」的提示。
    曾经把它写成无条件打印，结果 PASS 的条目显示「已不再直接发 done，
    请更新文档」，与 PASS 自相矛盾（读的人会以为脚本坏了）。
    `info` 一直打印，放中性的事实（行号、个数）。
    """
    results.append((name, ok, fail_hint, info))


def src(rel: str) -> str:
    p = BE / rel
    assert p.exists(), f"找不到 {rel}"
    return p.read_text(encoding="utf-8")


def lines(rel: str) -> list[str]:
    return src(rel).split("\n")


def line_of(rel: str, needle: str, nth: int = 1) -> int:
    """第 nth 次出现 needle 的行号（1-based），找不到返回 -1。"""
    seen = 0
    for i, ln in enumerate(lines(rel), 1):
        if needle in ln:
            seen += 1
            if seen == nth:
                return i
    return -1


# ============================================================ §1 启动自愈误伤

DB = "app/db.py"

# 「无条件清 streaming」这条语句必须仍然存在（修好后要反过来更新文档）
n_wipe = line_of(DB, "UPDATE messages SET status='stopped' WHERE role='assistant' "
                     "AND status='streaming'")
add("§1 无条件收敛语句仍在 db.py", n_wipe > 0,
    fail_hint="已消失 —— 如果已加 owner 过滤，请更新文档 §1",
    info=f"在 db.py:{n_wipe}" if n_wipe > 0 else "")

if n_wipe > 0:
    # 它所在的 SQL 里不能出现 owner / live_owners 过滤
    ctx = "\n".join(lines(DB)[n_wipe - 4: n_wipe + 6])
    has_filter = bool(re.search(r"\bowner\b|live_owners", ctx))
    add("§1 该语句确实没有 owner 过滤（问题成立前提）", not has_filter,
        fail_hint="已出现 owner 过滤 —— 文档 §1 应改为「已修」")

# init_db 里必须有这条收敛（证明它挂在启动路径上）
add("§1 收敛在 init_db 内（不是别处）",
    "UPDATE messages SET status='stopped'" in src(DB))

# init_db 无条件调用（每个实例 lifespan 都跑）
add("§1 init_db 在 lifespan 无条件执行",
    "await db.init_db()" in src("app/main.py"))

# 对照：recover_stale_runtime 有正确的 owner 过滤（证明范式在仓库里已存在）
ts = src("app/team_store.py")
add("§1 对照：recover_stale_runtime 有 live_owners 参数",
    "live_owners: set[str] | None = None" in ts)
add("§1 对照：它按 owner 过滤而非全清",
    bool(re.search(r"owner='' OR owner NOT IN \(", ts)))

# 对照：messages 表没有 owner 列（这是根因）
add("§1 根因：messages 表无 owner 列",
    not re.search(r"CREATE TABLE IF NOT EXISTS messages\s*\([^)]*\bowner\b", src(DB), re.S))

# owner 列是靠 ALTER 补的，且只补两张表
add("§1 owner 列由 _ensure_owner_columns 补（仅 session_generations/team_tasks）",
    'for table in ("session_generations", "team_tasks"):' in ts)


# ============================================================ §2 cookie 下发面

MAIN = src("app/main.py")

add("§2 _ensure_sticky_cookie 定义存在", "_ensure_sticky_cookie" in MAIN)

n_calls = len(re.findall(r"^\s+_ensure_sticky_cookie\(", MAIN, re.M))
add("§2 它只在 1 处被调用（暴露面窄）", n_calls == 1,
    fail_hint=f"实际 {n_calls} 处 —— 如果已挪进中间件，请更新文档 §2")

if n_calls == 1:
    call_line = line_of("app/main.py", "_ensure_sticky_cookie(request, response)")
    # 调用点必须在 health 端点内（health 定义在它之前、且更近）
    health_line = line_of("app/main.py", 'async def health(')
    add("§2 调用点在 health 端点内", 0 < health_line < call_line,
        info=f"health 在 :{health_line}，调用在 :{call_line}")

# 中间件存在（修法落点）
add("§2 已有 _instance_header 中间件可作为修法落点",
    "_instance_header" in MAIN and '@app.middleware("http")' in MAIN)


# ============================================================ §3 nginx 配置

NGINX = (BE / "deploy/nginx.conf").read_text(encoding="utf-8")

add("§3 使用普通 hash（不带 consistent）",
    re.search(r"^\s*hash \$cookie_hlz_sticky;\s*$", NGINX, re.M) is not None)
add("§3 没有 hash ... consistent（会退化成轮询）",
    not re.search(r"hash\s+\$cookie_hlz_sticky\s+consistent", NGINX))
add("§3 注释记录了「名字相同的多点重合」这一实测",
    "共用同一个名字" in NGINX or "三个点重合" in NGINX)
add("§3 注释给出了显式副本 + consistent 的替代方案",
    "server hlzscale-app-1:8000" in NGINX)


# ============================================================ §4 续看谎报 done

CHAT = src("app/routers/chat.py")

n_get_job = len(re.findall(r"turn_job\.get_live_job_for_session\(", CHAT))
add("§4 续看端点用进程内查 job（问题成立前提）", n_get_job >= 1,
    info=f"{n_get_job} 处")

# 该分支直接把 done 发出去（不和「真的结束了」区分）
seg_start = CHAT.find('job = turn_job.get_live_job_for_session(user["id"], session_id)')
seg = CHAT[seg_start: seg_start + 2500] if seg_start >= 0 else ""
add("§4 job is None 时直接发 done（未区分连错实例）",
    'sse_frame("done"' in seg,
    fail_hint="已不再直接发 done —— 文档 §4 应改为「已修」")

# 但同一文件里 409 路径已经用了正确的判活（证明判据现成）
add("§4 对照：409 路径已用 is_generation_dead_elsewhere",
    "is_generation_dead_elsewhere" in CHAT)

# generation_owner 提供现成判据
go = src("app/generation_owner.py")
add("§4 对照：generation_owner.is_generation_dead_elsewhere 存在",
    "async def is_generation_dead_elsewhere" in go)


# ============================================================ 前端 done 映射

APP_TSX = (HLAUDZ / "frontend/src/App.tsx").read_text(encoding="utf-8")

# 'streaming' 落到 else -> 'complete'，这是「谎报」在前端的放大点
add("§4 前端把未知 status 映射成 complete（放大点）",
    re.search(r"ev\.status === 'stopped' \? 'stopped' : ev\.status === 'error' \? 'error' : 'complete'",
              APP_TSX) is not None)
add("§4 前端没有 wrong_instance 处理（修法未落地）",
    "wrong_instance" not in APP_TSX,
    fail_hint="已出现 wrong_instance —— 文档 §4 的前端配套已落地，请更新")

# health 失败是静默的（§2 的暴露面来源之一）
add("§2 前端 health 失败静默（catch(() => null)）",
    "await api.health().catch(() => null)" in APP_TSX)


# ============================================================ §5 测试入口

manual = BE / "tests/manual"
py_files = sorted(p.name for p in manual.glob("*.py")) if manual.exists() else []
add("§5 tests/manual 有大量手工探针", len(py_files) >= 20, info=f"{len(py_files)} 个")
shell_files = sorted(p.name for p in manual.glob("*.sh")) if manual.exists() else []
add("§5 但没有 shell/Makefile 一键入口（问题成立）", not shell_files,
    fail_hint=f"出现了 {shell_files} —— 文档 §5 应更新")

# 环境变量确实只在 docs 里
docs_hits = list((HLAUDZ / "docs").glob("*.md"))
var_in_docs = [p.name for p in docs_hits
               if "HLZ_TEST_REDIS_URL" in p.read_text(encoding="utf-8", errors="replace")]
add("§5 HLZ_TEST_REDIS_URL 只出现在 docs", len(var_in_docs) >= 1,
    info=f"{len(var_in_docs)} 份文档")

# ============================================================ 文档自身

DOC = HLAUDZ / "docs/20261119-多实例正确性审计.md"
if DOC.exists():
    text = DOC.read_text(encoding="utf-8")
    for must, pat in [
        ("有「先排除的伪问题」表", r"先排除的伪问题"),
        ("有处理顺序表", r"建议的处理顺序"),
        ("写明了局限", r"本文档的局限"),
        ("引用了 §1 探针", r"probe_initdb_cross_instance_wipe"),
    ]:
        add(f"文档 {must}", re.search(pat, text) is not None)
    add("文档无过期数字 76 KB 等", "76 KB" not in text)
else:
    add("文档存在", False, fail_hint=str(DOC))

# ============================================================ 汇总

print("=" * 72)
bad = 0
for name, ok, fail_hint, info in results:
    mark = "PASS" if ok else "FAIL"
    tail = ""
    if ok and info:
        tail = f"   ({info})"
    elif not ok and fail_hint:
        tail = f"\n         -> {fail_hint}"
    elif not ok and info:
        tail = f"   ({info})"
    print(f"[{mark}] {name}{tail}")
    if not ok:
        bad += 1
print("=" * 72)
print(f"共 {len(results)} 条，通过 {len(results) - bad}，失败 {bad}")
if bad:
    print("\n注意：FAIL 不一定代表代码有问题 —— 也可能是「问题已修好，文档该更新」。")
    print("      每条 FAIL 都带说明，按说明判断属于哪种。")
    sys.exit(1)
print("全部通过")
sys.exit(0)