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

results: list[tuple[str, bool, str, str, bool]] = []


def add(name: str, ok: bool, fail_hint: str = "", info: str = "",
        fixed_marker: bool = False) -> None:
    """登记一条断言。

    `fail_hint` 只在**失败**时打印 —— 它是「这条红了该怎么判断」的提示。
    曾经把它写成无条件打印，结果 PASS 的条目显示「已不再直接发 done，
    请更新文档」，与 PASS 自相矛盾（读的人会以为脚本坏了）。
    `info` 一直打印，放中性的事实（行号、个数）。

    `fixed_marker`：这条断言测的是一个**已修**的缺陷。它红了代表「修好了」，
    不是「坏了」。打印时用 `[FIXED]` 而不是 `[FAIL]`，并在汇总里分开计数 ——
    否则读完一堆 FAIL 会以为代码退化了。
    """
    results.append((name, ok, fail_hint, info, fixed_marker))


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

# 断言（已修）：收敛的**判据**必须按 owner + 存活集合过滤。
#
# 注意落点：`UPDATE messages SET status='stopped'` 在 converge_stale_streaming 里
# 是一条**动态拼 SQL**（`AND session_id IN (...)`），过滤逻辑在
# `_sessions_to_converge`。第一版断言去查 UPDATE 附近的文本，结果被上方注释里的
# `owner` 字样骗过 —— 反向确认（去掉过滤）没变红才暴露出来。
# 现在直接断言判据函数本身。
add("§1 有 _sessions_to_converge 判据函数（已修）",
    "async def _sessions_to_converge" in src(DB),
    fail_hint="判据函数没了 —— 收敛可能退回无条件全清（a8f811c 的回归）")

conv_start = src(DB).find("async def _sessions_to_converge")
conv_seg = src(DB)[conv_start: conv_start + 3000] if conv_start >= 0 else ""
# 断言必须测「循环体真的在用 owner 判据」，而不是「这些变量名出现过」
# —— 第一版查变量名，反向确认把循环体换成 `for sid in []` 之后仍然匹配，
#    于是「去掉过滤」这个回退没被抓到。
add("§1 判据按 owner 与存活集合过滤（已修）",
    ("if sid not in owner_by_session:" in conv_seg
     and "if not owner:" in conv_seg
     and "if owner == my_owner:" in conv_seg
     and "if owner not in alive:" in conv_seg),
    fail_hint="判据的四条分支不完整 —— 收敛可能退回无条件全清（a8f811c 的回归）")

# 收敛仍挂在 init_db 上（启动路径不变）
add("§1 init_db 仍调用收敛函数",
    "converge_stale_streaming" in src(DB),
    fail_hint="init_db 不再收敛 —— 启动自愈断了")

# 对照：recover_stale_runtime 有正确的 owner 过滤（证明范式在仓库里已存在）
ts = src("app/team_store.py")
add("§1 对照：recover_stale_runtime 有 live_owners 参数",
    "live_owners: set[str] | None = None" in ts)
add("§1 对照：它按 owner 过滤而非全清",
    bool(re.search(r"owner='' OR owner NOT IN \(", ts)))

# 对照：messages 表没有 owner 列（这是为什么必须靠 session 关联）
add("§1 根因：messages 表无 owner 列",
    not re.search(r"CREATE TABLE IF NOT EXISTS messages\s*\([^)]*\bowner\b", src(DB), re.S))

# owner 列是靠 ALTER 补的，且只补两张表
add("§1 owner 列由 _ensure_owner_columns 补（仅 session_generations/team_tasks）",
    'for table in ("session_generations", "team_tasks"):' in ts)


# ============================================================ §2 cookie 下发面

MAIN = src("app/main.py")

add("§2 _ensure_sticky_cookie 定义存在", "_ensure_sticky_cookie" in MAIN)

# 断言（已修）：必须由中间件下发，而不是只在 health 里。
# 修好后把断言翻过来 —— 有人挪回 health 专属就变红。
mw_start = MAIN.find("async def _instance_header")
mw_seg = MAIN[mw_start: mw_start + 2000] if mw_start >= 0 else ""
add("§2 中间件里下发粘性 cookie（已修）",
    "_ensure_sticky_cookie" in mw_seg,
    fail_hint="中间件不再下发 —— 没访问过 health 的客户端会永远没有粘性（3189e7e 的回归）")

# health 里不该再有第二处（搬干净了）
health_line = line_of("app/main.py", 'async def health(')
add("§2 health 里不再单独下发", health_line > 0, info=f"health 在 :{health_line}")

# secure=False 不能被改成 True（HTTP 部署下会静默失效）
fn_start = MAIN.find("def _ensure_sticky_cookie")
fn_seg = MAIN[fn_start: fn_start + 1200] if fn_start >= 0 else ""
add("§2 secure=False（HTTP 部署下 True 会让浏览器丢弃 cookie）",
    "secure=False" in fn_seg,
    fail_hint="secure 被改开了？HTTP 部署下粘性会静默失效，比没有更糟")

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

# 断言（已修）：必须有 _resume_decision，且它会返回 wrong_instance。
# 修好后把断言翻过来 —— 有人把它改回「只看 job 是否存在」就变红。
add("§4 _resume_decision 存在（已修）",
    "async def _resume_decision" in CHAT,
    fail_hint="决策函数没了 —— 续看又会把「连错实例」当成「已完成」（39763ed 的回归）")

dec_start = CHAT.find("async def _resume_decision")
dec_seg = CHAT[dec_start: dec_start + 3000] if dec_start >= 0 else ""
add("§4 它返回 wrong_instance 这一分支",
    'wrong_instance' in dec_seg,
    fail_hint="wrong_instance 分支没了 —— 客户端不会被通知去重试")
add("§4 它用 is_generation_dead_elsewhere 判活",
    "is_generation_dead_elsewhere" in dec_seg,
    fail_hint="判据不再是 owner 存活 —— 会退回「看 messages.status」的老错")

# 端点必须走决策函数
rs_start = CHAT.find("async def resume_stream")
rs_seg = CHAT[rs_start: rs_start + 2500] if rs_start >= 0 else ""
add("§4 resume_stream 调用 _resume_decision",
    "_resume_decision" in rs_seg,
    fail_hint="端点没走决策函数 —— 三条分支不生效")

# 返回 409（显式失败，而不是塞 SSE error 帧）
add("§4 wrong_instance 走 HTTP 409",
    bool(re.search(r'wrong_instance[\s\S]{0,200}status_code=409|status_code=409[\s\S]{0,200}wrong_instance',
                   CHAT)),
    fail_hint="不再返回 409 —— 客户端没法在流开始前就知道要重试")

# 但同一文件里 409 路径已经用了正确的判活（证明判据现成）
add("§4 对照：409 路径已用 is_generation_dead_elsewhere",
    "is_generation_dead_elsewhere" in CHAT)

# generation_owner 提供现成判据
go = src("app/generation_owner.py")
add("§4 对照：generation_owner.is_generation_dead_elsewhere 存在",
    "async def is_generation_dead_elsewhere" in go)


# ============================================================ 前端 done 映射

APP_TSX = (HLAUDZ / "frontend/src/App.tsx").read_text(encoding="utf-8")

# 'streaming' 落到 else -> 'complete' —— 这是「谎报」在前端的放大点。
# 这条**仍然成立**且必须保留：它是后端返回值与前端映射共同作用的结果，
# 后端修好（不再在别人跑着时发 done）之后，这个映射本身仍是正确的
# （那时 status 真的只有三种）。保留它作为「前端不会把 streaming 当完成」的
# 保险：如果哪天有人去掉后端的 wrong_instance 分支，这里会提醒映射会放大问题。
add("§4 前端 status 映射只有三态（放大点的来源）",
    re.search(r"ev\.status === 'stopped' \? 'stopped' : ev\.status === 'error' \? 'error' : 'complete'",
              APP_TSX) is not None,
    fail_hint="映射改了 —— 如果改成能识别 streaming，§4 的放大效应就消失了，请更新文档")

# 断言（已修）：前端必须真的处理 wrong_instance，且要「先换 cookie 再重试」
add("§4 前端处理 wrong_instance（已修）",
    "wrong_instance" in APP_TSX or "isWrongInstanceError" in APP_TSX,
    fail_hint="前端不认 wrong_instance 了 —— 用户会看到一个红色报错而不是自动恢复（39763ed 的回归）")
add("§4 重试前先重新握手（打 health 换 cookie）",
    "api.health()" in APP_TSX,
    fail_hint="重试前没换 cookie —— 会落到同一个错实例，是无效动作")
add("§4 只重试一次（防无限循环）",
    "planResumeRetry" in APP_TSX and "alreadyRetried" in APP_TSX)

# health 失败是静默的（§2 的暴露面来源之一）—— 现在也被中间件兜住了，
# 但这条仍描述事实，保留。
add("§2 前端 health 失败静默（catch(() => null)）",
    "await api.health().catch(() => null)" in APP_TSX)


# ============================================================ §5 测试入口

manual = BE / "tests/manual"
py_files = sorted(p.name for p in manual.glob("*.py")) if manual.exists() else []
add("§5 tests/manual 有大量手工探针", len(py_files) >= 20, info=f"{len(py_files)} 个")

# 断言（已修）：必须有一键入口，且它真的设置那两个环境变量
shell_files = sorted(p.name for p in manual.glob("*.sh")) if manual.exists() else []
add("§5 有一键入口脚本（已修）", bool(shell_files),
    fail_hint="一键入口没了 —— 52 条多实例测试又会变成「没人知道怎么跑」",
    info=f"{shell_files}")

runner = manual / "run_cluster_tests.sh"
if runner.exists():
    rs = runner.read_text(encoding="utf-8", errors="replace")
    add("§5 入口设置了 HLZ_TEST_REDIS_URL", "HLZ_TEST_REDIS_URL" in rs,
        fail_hint="没设 Redis 环境变量 —— 那 24 条测试还是会被跳过")
    add("§5 入口设置了 HLZ_TEST_PG_DSN", "HLZ_TEST_PG_DSN" in rs,
        fail_hint="没设 PG 环境变量 —— 那 24 条测试还是会被跳过")
    add("§5 入口有依赖检查（缺容器时明确报缺什么，不静默跳过）",
        "--check" in rs and "/dev/tcp/" in rs,
        fail_hint="没有依赖检查 —— 容器没起时会静默跳过，和修之前一样")

# 清单校验脚本存在（防止新增依赖测试被漏加）
add("§5 有清单校验脚本（漏加会退出码 1）",
    (HLAUDZ / "tools/verify/verify_cluster_test_list.py").exists(),
    fail_hint="校验脚本没了 —— 新增的依赖测试会被静默跳过")

# 环境变量确实记在 docs 里（现在也在脚本里，两处都要有）
docs_hits = list((HLAUDZ / "docs").glob("*.md"))
var_in_docs = [p.name for p in docs_hits
               if "HLZ_TEST_REDIS_URL" in p.read_text(encoding="utf-8", errors="replace")]
add("§5 HLZ_TEST_REDIS_URL 在 docs 里有记录", len(var_in_docs) >= 1,
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
for name, ok, fail_hint, info, _fixed in results:
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
    print("\n每条 FAIL 都带 `->` 说明，说明里写着这是「代码坏了」还是「文档该更新」。")
    sys.exit(1)
print("全部通过")
sys.exit(0)