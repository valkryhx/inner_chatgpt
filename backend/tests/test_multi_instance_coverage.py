"""多实例覆盖面的清单断言（2026-11-19）。

## 为什么要有这份测试

「多实例做完了吗」这个问题只有两种答案有意义：**某处状态在 Redis 里（跨实例）**
还是**在进程字典里（单实例）**。凭记忆回答一定会漂，所以把每个子系统的
判定依据钉成断言，谁改了实现就会在这里变红。

## 判定方法

对每个子系统，找它的**权威状态存放点**：
- 有 `cluster.*` 调用 或 直接读写 Redis  -> 多实例
- 只有模块级 `dict` / `set`             -> 单实例
- 状态在数据库里但**没有 owner 列**      -> 部分（写入共享但收敛是全局的）

否定结论（「X 没有 Redis 调用」）写成 `must_not`，每次重跑。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def strip_comments(text: str) -> str:
    """去掉 Python 里 `#` 之后的内容（逐行，够用即可）。

    **必须有这一步**：注释里出现 `from .cluster import` 是完全合法的
    （比如「原来是 `from .cluster import stopsignal`，现在改成…」），
    不剥注释的话，把真代码注释掉仍然能被正则匹配到 —— 断言会在
    错误的原因下通过，等于没测。本仓库已踩过这个坑（AGENTS.md §8.6）。
    """
    out = []
    for line in text.split("\n"):
        # 简单处理：不解析字符串里的 #（这些源码里不会出现，够用）
        idx = line.find("#")
        out.append(line if idx < 0 else line[:idx])
    return "\n".join(out)


def src(rel: str) -> str:
    p = BACKEND / "app" / rel
    assert p.exists(), f"找不到 {rel}"
    return p.read_text(encoding="utf-8")


def src_live(rel: str) -> str:
    """剥掉注释后的源码 —— 做「有没有真的接 cluster」这类断言必须用它。"""
    return strip_comments(src(rel))


def uses_cluster(rel: str) -> bool:
    """该模块是否**真的**调用了 cluster 层（注释不算）。"""
    t = src_live(rel)
    return bool(re.search(r"from\s+\.+cluster\s+import|cluster\.(stopsignal|steerbus|"
                          r"invalidate|instances|lock|leader)", t))


# ============================================================ 已多实例

@pytest.mark.parametrize("rel,why", [
    ("runtime.py", "停止信号 stopsignal + 缓存失效 invalidate"),
    ("steer_queue.py", "插话总线 steerbus"),
    ("generation_owner.py", "跨实例 generation 判活"),
    ("team_store.py", "owner 列 + 存活集合过滤"),
    ("sandbox/manager.py", "启动清扫 leader 租约 + live_owners"),
    ("sandbox/probe.py", "清扫按 owner 过滤"),
    ("sandbox/identity.py", "owner 含实例标识"),
    ("main.py", "实例登记 + 心跳 + health 集群视图"),
])
def test_module_uses_cluster_layer(rel: str, why: str):
    """这些模块必须真的接入了 cluster 层（不是仅注释里提到）。"""
    assert uses_cluster(rel), f"{rel} 没有接入 cluster 层（{why}）"


def test_locks_are_scoped():
    """写事务的锁必须带 scope，否则所有队的写排成一队。

    注意 scope 是**字符串拼接**进来的（`"BEGIN IMMEDIATE " + f"gen:{...}"`），
    不是字面量，所以按 `BEGIN IMMEDIATE` 的出现次数数，再确认带 scope 的
    占绝大多数。

    **有一处刻意不带 scope**：`recover_stale_runtime()` 要扫**所有 team**，
    用全局锁是正确的（带 scope 就得为每个 team 各加一次锁，反而更慢且
    无法保证跨 team 一致）。它是启动期一次性动作，串行化代价可忽略。
    """
    t = src("team_store.py")
    begins = re.findall(r"BEGIN IMMEDIATE([^\n]*)", t)
    assert len(begins) >= 5, \
        f"只有 {len(begins)} 处 BEGIN IMMEDIATE，期望 >= 5（分锁覆盖的业务）"
    scoped = [b for b in begins if '"' in b and "+" in b]
    global_ = [b for b in begins if '"' not in b]
    assert len(scoped) >= 5, f"带 scope 的只有 {len(scoped)} 处，期望 >= 5"
    assert len(global_) <= 1, \
        f"有 {len(global_)} 处全局锁，只有 recover_stale_runtime 该用全局锁"


def test_ddl_has_cross_instance_lock():
    """多副本同时建表必须串行化（PG 的 IF NOT EXISTS 非原子）。"""
    t = src("pgdriver.py")
    assert "DDL_LOCK_SCOPE" in t
    assert "pg_advisory_xact_lock" in t or "pg_try_advisory_xact_lock" in t


# ============================================================ 单实例（进程内）

@pytest.mark.parametrize("rel,marker,why", [
    ("turn_job.py", "_JOBS",
     "SSE 订阅与缓冲在进程内 —— 客户端必须连到跑生成的那个实例"),
    ("memory_extract.py", "_counters",
     "每轮记忆抽取的计数器在进程内"),
    ("memory_extract.py", "_inflight",
     "抽取去重（coalesce）只有进程内合并，跨进程无锁"),
])
def test_process_local_state_is_in_module_dict(rel: str, marker: str, why: str):
    """这些状态**仍然**在进程内。有人把它外置时这条会红，提醒同步更新文档。"""
    t = src(rel)
    assert re.search(rf"^{re.escape(marker)}\b", t, re.M), \
        f"{rel} 里找不到 {marker}（{why}）"


def test_subagent_registry_has_no_cluster_wiring():
    """子 agent 注册表没有接入 cluster（must_not）。

    它有 DB 侧链表（subagent/store.py）用于**重启后**收敛，
    但没有 owner 列、也没有 Redis 状态 —— 所以是「孤儿收敛」，不是跨实例协调。
    """
    t = src_live("subagent/registry.py")
    assert "cluster" not in t and "redis" not in t.lower(), \
        "subagent/registry.py 出现了 cluster/redis —— 请更新本文档与调查报告"


def test_turn_job_has_no_cluster_wiring():
    """turn_job 没有接入 cluster（must_not）。"""
    t = src_live("turn_job.py")
    assert "cluster" not in t and "redis" not in t.lower(), \
        "turn_job.py 出现了 cluster/redis —— 请更新本文档与调查报告"


def test_memory_dream_has_no_cluster_wiring():
    """dream 巡检没有 cluster 接入（must_not）。它有 PID + CAS 文件锁，
    那只做**同机**互斥，不跨实例。"""
    t = src_live("memory_dream.py")
    assert "cluster" not in t, "memory_dream.py 出现了 cluster —— 请更新文档"


# ============================================================ 部分

def test_team_runs_lacks_owner_column():
    """`team_runs` 表没有 owner 列 —— recover 时是全局收敛（已知限制）。"""
    t = src("team_store.py")
    # 有这个表
    assert "team_runs" in t
    # 它的建表语句里不应有 owner（有就说明这条限制已解除，文档要改）
    m = re.search(r"CREATE TABLE IF NOT EXISTS team_runs\s*\((.*?)\)", t, re.S)
    if m:
        assert "owner" not in m.group(1), \
            "team_runs 已有 owner 列 —— 请更新「已知限制」"


def test_recover_stale_runtime_uses_live_owners():
    """`recover_stale_runtime` 必须接收存活集合，不能无差别收敛。

    用 `\\blive_owners\\b`（词边界）：否则把标识符改名成 `live_owners_renamed`
    仍会被子串匹配到 —— 断言会在错误的原因下通过。
    """
    t = src_live("team_store.py")
    assert re.search(r"\blive_owners\b", t), \
        "recover_stale_runtime 没有接收 live_owners"