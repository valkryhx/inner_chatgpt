"""S-M6 / S-M7：选择器的量化收益测量（真 LLM）。

## 为什么必须有这个工具

方案 §3.7 明确：M3.5「**不是必然要做**，必须先量清楚它对命中率和 TTFT 的影响
再决定是否打开」。CC 自己的默认形态也不跑选择器。

所以这里不测「选择器能不能工作」（那是 L1 单测），只测两个数：

- **S-M6 命中率**：同一组记忆 + 同一批查询，关键词排序 vs LLM 选择器，
  谁的 top-5 更接近人工标注的理想答案。
- **S-M7 TTFT**：`SELECTOR=0` 与 `=1` 的首 token 时间差。

## 为什么数据集要「人工标注」

关键词命中的根本问题是「不知道用户在问什么」。要证明选择器更好，
必须构造**没有字面重合**的用例 —— 否则两者都对，测不出差异。

这批用例就是为此设计的：每条查询与目标记忆**没有共同关键词**。
比如查询「上次那个报错怎么解决的」，目标记忆的描述是「构建失败的处理流程」。
关键词给 0 分，选择器应该能靠语义连上。

## 跑法

```bash
# 后端要先起（python run.py），且该用户配了可用模型
cd backend
python ../tools/memory_verify/selector_bench.py                # 全部
python ../tools/memory_verify/selector_bench.py --case m6      # 只测命中率
python ../tools/memory_verify/selector_bench.py --case m7      # 只测 TTFT
```

输出 JSON 到 `work/selector_bench_report.json`，含逐条明细（哪条查询、
关键词选了哪些、选择器选了哪些、理想答案是什么）。

**注意成本**：`--case m6` 会跑 N 个查询 × 2 种策略。默认 8 个查询
= 8 次选择器调用 + 8 次主模型调用（关键词侧不需要模型，但要建对照）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

# ---------------- 数据集 ----------------
#
# 设计原则：**查询与目标记忆之间没有字面重合**。
# 这是唯一能拉开「关键词排序」与「语义选择」差距的构造。
# 每条都写明「为什么关键词会漏」，否则以后没法判断这个用例是否还有效。

MEMORIES = [
    ("build-failure", "构建失败时的排查顺序：先看 lockfile 再清缓存",
     "project", ["构建", "依赖"]),
    ("db-lock", "SQLite 并发写入遇到 database is locked 的处置",
     "project", ["SQLite", "并发"]),
    ("user-tone", "用户要求技术讨论先给结论和数据，不写背景铺垫",
     "feedback", ["沟通"]),
    ("user-call-name", "用户希望被称呼为「老徐」",
     "user", ["称呼"]),
    ("deploy-window", "生产发布只能在工作日 10:00-16:00 之间做",
     "project", ["发布"]),
    ("test-flake", "process_registry 的停止测试在负载高时会偶发失败",
     "project", ["测试"]),
    ("api-retry", "上游 502 时的重试与降级策略见 HLZ_LLM_MAX_RETRIES",
     "reference", ["LLM"]),
    ("board-url", "迭代看板在 https://example.invalid/board",
     "reference", ["看板"]),
    ("dream-rule", "dream 只整理已有记忆，不新增本轮事实",
     "project", ["记忆"]),
    ("pty-note", "Windows 上 SelectorEventLoop 不能起子进程，要用 Proactor",
     "project", ["Windows"]),
    ("cache-clear", "改完 skill 要重启后端才会重新加载提示词",
     "project", ["Skill"]),
    ("legacy-note", "旧版 langchain 的 checkpoint 迁移已完成，不必再看",
     "project", ["迁移"]),
    # ---- 干扰项：与上面 8 条查询**语义无关**，但用词相近 ----
    # 它们的作用是把 top-5 的分母撑大，让「关键词瞎排也能撞上」失效。
    # 没有这批，12 条记忆时 top5 就占 42%，基准虚高、差距虚小。
    ("git-hook", "pre-commit 钩子会跑 ruff 与 eslint，失败则拒绝提交",
     "project", ["Git"]),
    ("env-file", "本地 .env 不进 git，改完要重启后端才生效",
     "project", ["配置"]),
    ("port-map", "8000 是后端，5173 是 vite dev，别混",
     "reference", ["端口"]),
    ("db-backup", "改 schema 前先备份 app.db 连同 -wal 与 -shm",
     "project", ["数据库"]),
    ("win-bash", "外层 PowerShell、内层 Git Bash，shell 参数不要写 bash",
     "feedback", ["终端"]),
    ("utf8-rule", "写文件一律 UTF-8 无 BOM、LF 换行",
     "feedback", ["编码"]),
    ("token-budget", "注入预算按 code point 计，索引 cap 也是 code point",
     "project", ["预算"]),
    ("recall-bytes", "单文件召回上限用的是真字节，与索引口径不同",
     "project", ["预算"]),
    ("sse-contract", "SSE 事件名与字段是稳定契约，不许破坏",
     "feedback", ["协议"]),
    ("subagent-scope", "子代理不能递归 spawn，编排工具只注入 root",
     "project", ["子代理"]),
    ("team-lease", "generation 租约过期要走自愈，不能永久锁死",
     "project", ["Team"]),
    ("a2a-tier", "A2A 权限档位建 token 时定死：full/read_only/chat_only",
     "reference", ["A2A"]),
    ("mcp-ttl", "MCP 连接有软 TTL，会话僵死要能自动重连",
     "project", ["MCP"]),
    ("acp-bridge", "GA 桥进程要注册 ProcessLease，停止走优雅 cancel 再硬杀",
     "project", ["ACP"]),
    ("skill-layers", "skill 三层：builtin 只读 / shared 公用 / user 私有",
     "project", ["Skill"]),
    ("workspace-iso", "文件工具限制在 workspaces/<user_id>/，越界要拒绝",
     "project", ["隔离"]),
    ("first-frame", "首帧串行阻塞的硬指标是 300ms",
     "project", ["性能"]),
    ("compaction-soft", "软线压缩在首帧前调 LLM，会打穿首帧预算",
     "project", ["压缩"]),
    ("stop-cascade", "停止要级联：主 stop -> 子代理 -> code_run",
     "project", ["停止"]),
    ("image-limit", "单条消息最多 4 张图，单张 25MB",
     "reference", ["图片"]),
    ("memory-kinds", "记忆四类：user/feedback/project/reference",
     "project", ["记忆"]),
    ("delete-gate", "dream 删条目要先读后删，读后变了要冲突",
     "project", ["记忆"]),
    ("flaky-test", "test_code_run_stop_kills_child 在负载高时偶发失败",
     "project", ["测试"]),
    ("pytest-scope", "pytest-asyncio 的 fixture loop scope 未设置会告警",
     "reference", ["测试"]),
    ("node-version", "node 用 25.x，npm run build 前先确认 node_modules 在",
     "reference", ["前端"]),
    ("playwright-path", "playwright 未装时 pip install 后还要 install chromium",
     "reference", ["前端"]),
    ("mutation-test", "变异测试：把机制改坏，看对应测试是否变红",
     "project", ["测试"]),
    ("index-owner", "MEMORY.md 的唯一改写者是 dream，提取器不碰",
     "project", ["记忆"]),
]

# (查询, 理想 top-3 slug, 这条为什么难)
QUERIES = [
    ("上次那个装了新依赖之后就起不来的问题，后来是怎么弄好的？",
     ["build-failure"], "查询里没有「构建」二字，关键词给 0"),
    ("两个人同时保存的时候报的那个错的原文是什么来着",
     ["db-lock"], "「同时保存」vs「并发写入」，无共同词"),
    ("跟你讲技术方案的时候我希望你怎么组织内容",
     ["user-tone"], "「组织内容」vs「先给结论」，无共同词"),
    ("以后你叫我什么来着",
     ["user-call-name"], "指代型问句，无字面线索"),
    ("线上能不能下午再发版",
     ["deploy-window"], "「发版」vs「发布」，1 字重合但语义关键的是时间窗"),
    ("那个偶尔红一下的测试是哪个文件里的",
     ["test-flake"], "「偶尔红一下」vs「偶发失败」，无共同词"),
    ("接口挂了会重试几次",
     ["api-retry"], "「接口挂了」vs「上游 502」，无共同词"),
    ("我们这轮要做什么，去哪儿看",
     ["board-url"], "「去哪儿看」指向看板，但查询没有「看板」"),
]


def _prepare(tmp: Path):
    from app import db, memory_store as ms

    ms.MEMORIES_DIR = tmp / "memories"
    ms.DB_PATH = tmp / "app.db"
    db.DB_PATH = tmp / "app.db"
    return ms, db


async def _seed(ms, user: str) -> None:
    for slug, desc, kind, tags in MEMORIES:
        await ms.write_memory(user, slug=slug, title=slug, description=desc,
                              body=f"# {desc}\n\n{desc}\n", kind=kind,
                              tags=tags)


def _keyword_top(user_specs, query: str, k: int) -> list[str]:
    """复现 `select_with_stats` 的排序，取前 k 个 slug。"""
    from app.memory_store import KIND_PRIORITY, _hit_score, _query_tokens

    tokens = _query_tokens(query)
    ranked = sorted(
        user_specs,
        key=lambda m: (-_hit_score(m, tokens),
                       KIND_PRIORITY.get(m.kind, 9),
                       -int(m.updated_at or 0), m.slug),
    )
    return [m.slug for m in ranked[:k]]


async def case_m6(ms, user: str, client, k: int = 5) -> dict:
    """S-M6：关键词 vs 选择器，top-k 命中率对比。"""
    from app import memory_prefetch as mp

    specs, _ = await ms.select_with_stats(
        user, char_budget=10 ** 7, max_entries=10 ** 4)
    rows = []
    kw_hits = sel_hits = 0
    for query, ideal, why in QUERIES:
        kw = _keyword_top(specs, query, k)
        picked = await mp.select_slugs(client, specs, query)
        ideal_set = set(ideal)
        kw_hit = len(ideal_set & set(kw))
        sel_hit = len(ideal_set & set(picked))
        kw_hits += kw_hit
        sel_hits += sel_hit
        rows.append({
            "query": query, "ideal": ideal, "why_hard": why,
            "keyword": kw, "selector": picked,
            "keyword_hit": kw_hit, "selector_hit": sel_hit,
        })
        print(f"  Q: {query[:28]}…  理想={ideal}")
        print(f"     关键词 top{k}={kw}  命中 {kw_hit}/{len(ideal)}")
        print(f"     选择器   top{k}={picked}  命中 {sel_hit}/{len(ideal)}")

    total_ideal = sum(len(x[1]) for x in QUERIES)
    return {
        "case": "S-M6",
        "k": k,
        "rows": rows,
        "keyword_hits": kw_hits,
        "selector_hits": sel_hits,
        "total_ideal": total_ideal,
        "keyword_rate": round(kw_hits / total_ideal, 3) if total_ideal else 0,
        "selector_rate": round(sel_hits / total_ideal, 3) if total_ideal else 0,
    }


async def case_m7(ms, user: str, client) -> dict:
    """S-M7：选择器开启时，预取不拖慢首帧。

    这里量的是**注入器/选择器自身的开销**，不是端到端 TTFT（那主要反映
    模型与网络）。媒体口径与 S-M5 一致：注入路径是模型调用**之前**同步跑的，
    所以它的耗时直接进 TTFT。
    """
    from app import memory_prefetch as mp

    # bench 工具自己把机制打开（生产里靠环境变量）。
    # 不打开的话 start_prefetch 一律返回 None，量到的永远是 0。
    os.environ["HLZ_MEMORY_SELECTOR"] = "1"
    os.environ.setdefault("HLZ_MEMORY_SELECTOR_MODEL", "bench")

    specs, _ = await ms.select_with_stats(
        user, char_budget=10 ** 7, max_entries=10 ** 4)
    query = QUERIES[0][0]

    samples: list[dict] = []
    for i in range(3):
        # 关键词路径（同步、零模型调用）
        t0 = time.monotonic()
        kw = _keyword_top(specs, query, 5)
        kw_ms = (time.monotonic() - t0) * 1000

        # 选择器路径：只量「启动」耗时（这才是进 TTFT 的部分），
        # 不算它算完要多久 —— 因为消费是下一轮的事。
        state = mp.PrefetchState()
        t1 = time.monotonic()
        handle = mp.start_prefetch(None, [{"role": "user", "content": query}],
                                   client=client, user_id=user, state=state)
        start_ms = (time.monotonic() - t1) * 1000
        settle_ms = None
        if handle is not None:
            t2 = time.monotonic()
            try:
                await asyncio.wait_for(handle.promise, timeout=60)
            except Exception:  # noqa: BLE001
                pass
            settle_ms = (time.monotonic() - t2) * 1000
        samples.append({
            "iter": i, "keyword_ms": round(kw_ms, 2),
            "prefetch_start_ms": round(start_ms, 2),
            "selector_settle_ms": round(settle_ms, 2) if settle_ms else None,
            "keyword_picked": len(kw),
            "prefetch_started": handle is not None,
        })

    starts = [s["prefetch_start_ms"] for s in samples]
    settles = [s["selector_settle_ms"] for s in samples if s["selector_settle_ms"]]
    return {
        "case": "S-M7",
        "samples": samples,
        "max_prefetch_start_ms": max(starts),
        "median_selector_settle_ms": sorted(settles)[len(settles) // 2] if settles else None,
        "prefetch_actually_started": any(s["prefetch_started"] for s in samples),
    }


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


BOOT = """import sys, pathlib
sys.path.insert(0, r"{backend}")
from app import config
config.DB_PATH = pathlib.Path(r"{db}")
import app.db as _db
_db.DB_PATH = pathlib.Path(r"{db}")
import app.history_store as _hs
_hs.DB_PATH = pathlib.Path(r"{db}")
import uvicorn
uvicorn.run("app.main:app", host="127.0.0.1", port={port}, reload=False, log_level="warning")
"""


def wait_health(base: str, timeout: float = 90) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            with urllib.request.urlopen(base + "/api/health", timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


def call(base, path, data=None, token=None):
    body = json.dumps(data).encode() if data is not None else None
    req = urllib.request.Request(base + path, data=body,
                                 method="POST" if body else "GET")
    if body:
        req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", "Bearer " + token)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode() or "{}")


async def main_async(args) -> int:
    tmp = Path(tempfile.mkdtemp(prefix="selector_bench_"))
    print(f"临时目录：{tmp}\n")
    try:
        ms, db = _prepare(tmp)
        await db.init_db()
        await ms.init_tables()
        user = "bench_user"
        await _seed(ms, user)
        print(f"已写入 {len(MEMORIES)} 条记忆，{len(QUERIES)} 条查询\n")

        client = await _pick_client(args)
        if client is None:
            print("找不到可用的模型配置。请先在界面里配一个 LLM，"
                  "或用 --base-url/--api-key/--model 显式指定。")
            return 1

        report: dict = {"started": time.time()}
        try:
            if args.case in ("all", "m6"):
                print("=== S-M6 命中率对比 ===")
                report["m6"] = await case_m6(ms, user, client, k=args.k)
                print(f"\n关键词命中率 {report['m6']['keyword_rate']:.1%}  "
                      f"选择器命中率 {report['m6']['selector_rate']:.1%}\n")
            if args.case in ("all", "m7"):
                print("=== S-M7 预取开销 ===")
                report["m7"] = await case_m7(ms, user, client)
                print(f"预取启动耗时上限 {report['m7']['max_prefetch_start_ms']:.1f}ms  "
                      f"选择器中位 settle {report['m7']['median_selector_settle_ms']}ms\n")
        finally:
            closer = getattr(client, "aclose", None)
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass

        out = ROOT / "work" / "selector_bench_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                       encoding="utf-8", newline="\n")
        print(f"报告：{out}")

        # 结论：选择器必须显著更好才值得默认打开
        m6 = report.get("m6")
        if m6:
            delta = m6["selector_rate"] - m6["keyword_rate"]
            print(f"\n命中率提升：{delta:+.1%}")
            if delta <= 0:
                print("⚠️  选择器没有更好 —— 按方案 §3.7，此时应保持默认关闭。")
            elif delta < 0.15:
                print("⚠️  提升有限（< 15 个百分点），考虑到每次模型调用的成本，"
                      "建议保持默认关闭。")
            else:
                print("✅ 提升显著，可作为可选增强提供给需要的用户（仍保持默认关闭）。")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


async def _pick_client(args):
    """拿一个可用的 LLM client：命令行参数 > 生产库里的默认配置。"""
    from app.llm import build_client_from_config

    if args.base_url and args.api_key and args.model:
        return build_client_from_config({
            "base_url": args.base_url, "api_key": args.api_key,
            "model": args.model, "wire_api": "openai_chat",
            "max_tokens": 4096, "context_window": 128000,
        })
    # 从生产库借一条默认配置（只读，不写入）
    import aiosqlite
    from app.config import DB_PATH

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_llm_configs WHERE api_key<>'' AND base_url<>'' "
            "ORDER BY is_default DESC, created_at ASC LIMIT 1")
        row = await cur.fetchone()
    if row is None:
        return None
    cfg = dict(row)
    print(f"借用配置：{cfg.get('model')} @ {cfg.get('base_url')}\n")
    return build_client_from_config(cfg)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", choices=["all", "m6", "m7"], default="all")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
