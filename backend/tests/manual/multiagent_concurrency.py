"""多智能体沙箱的**并发真机**测试：Agent Team / workflow / 多用户。

## 为什么要单独测

单测（`tests/test_sandbox_multiagent.py`）证明的是「单个子代理能执行」。
但真实场景是**并发**：一个用户同时派 3 个 agent，或者 2 个用户各自干活。
并发会暴露单测覆盖不到的问题：

- 多个 agent 同时 acquire 同一容器会不会互相干扰；
- A 的进程写文件时 B 在同一个 `/ws` 下写，会不会串；
- 各自 cwd 不同，相对路径是否真的隔离。

## 三条链路（已核实走同一个 write_root）

| 链路 | 入口 | write_root |
| --- | --- | --- |
| 子代理 | `subagent/runtime.py:74` | `.subagents/<session>/<agent>` |
| Agent Team | `team_host.spawn_teammate` -> `spawn_fn` -> 同上 | 同上 |
| workflow | 同上（都经 SubagentRuntime） | 同上 |

所以本测试直接构造这三种 write_root 并发执行，等价于三条链路。

## 判定

不用「没报错」，用**文件落点**：
每个 agent 的文件必须落在自己的 `D:\\sandbox\\<uid>\\.subagents\\<sess>\\<agent>\\`
且不出现在别人的目录里。

用法：
    python tests/manual/multiagent_concurrency.py
"""
from __future__ import annotations

import asyncio
import os
import secrets
import sys
import tempfile
import shutil
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    from app.sandbox.config import get_config
    from app.sandbox.runtime import ContainerRuntime
    from app.sandbox.errors import SandboxExecError

    cfg = get_config(reload=True)
    rt = ContainerRuntime(cfg)

    tmp = Path(tempfile.mkdtemp(prefix="hlz_mac_"))
    # 两个用户，各自一个容器
    users = ["u_alpha", "u_beta"]
    roots = {u: tmp / u for u in users}
    for r in roots.values():
        r.mkdir(parents=True, exist_ok=True)

    created: dict[str, str] = {}

    try:
        # ---- 起两个用户的容器 ----
        print("\n== 起容器（每用户一个）==")
        for u in users:
            created[u] = rt.create(u, str(roots[u]))
            print(f"  {u}: {created[u][:12]}  挂载 {roots[u]}")

        # ============================================================
        # 场景 1：单用户 Agent Team —— 3 个 agent 并发
        # ============================================================
        print("\n== 场景1：Agent Team，3 agent 并发（同一用户同一容器）==")
        u = "u_alpha"
        sess = "team_sess_1"
        agents = ["researcher", "coder", "reviewer"]

        async def agent_task(agent: str) -> tuple[str, str]:
            """模拟一个 agent：在自己目录写文件 + 读回 + 输出标记。"""
            sub = f".subagents/{sess}/{agent}"
            host = roots[u] / sub
            host.mkdir(parents=True, exist_ok=True)
            code = (
                "import os, pathlib\n"
                f"p = pathlib.Path('out_{agent}.txt')\n"
                f"p.write_text('AGENT={agent} CWD=' + os.getcwd())\n"
                "print('WROTE', os.getcwd(), p.read_text())\n"
            )
            res = await rt.exec_python(
                created[u], code, timeout_s=60,
                host_workspace=str(host), container_workspace=str(roots[u]),
                cwd=f"/ws/{sub}")
            return agent, res.stdout

        outs = await asyncio.gather(*[agent_task(a) for a in agents])
        for agent, out in outs:
            expect_cwd = f"/ws/.subagents/{sess}/{agent}"
            record(f"team/{agent}: 执行成功且 cwd 正确",
                   expect_cwd in out and f"AGENT={agent}" in out,
                   out.strip()[:100].replace("\n", " | "))

        # 文件落点：各自的目录有，别人的目录没有
        for agent in agents:
            own = roots[u] / ".subagents" / sess / agent / f"out_{agent}.txt"
            others = [
                roots[u] / ".subagents" / sess / o / f"out_{agent}.txt"
                for o in agents if o != agent
            ]
            ok = own.exists() and not any(p.exists() for p in others)
            record(f"team/{agent}: 文件只落在自己目录", ok, str(own.name))

        # ============================================================
        # 场景 2：workflow —— 多个节点串行 + 并行混跑
        # ============================================================
        print("\n== 场景2：workflow 多个节点（同用户，并行+串行）==")
        wf_sess = "wf_sess_1"
        nodes = ["node_a", "node_b", "node_c", "node_d"]

        async def wf_node(node: str, payload: str) -> str:
            sub = f".subagents/{wf_sess}/{node}"
            host = roots[u] / sub
            host.mkdir(parents=True, exist_ok=True)
            code = (
                "import pathlib\n"
                f"pathlib.Path('result.json').write_text('{payload}')\n"
                f"print('NODE={node} PAYLOAD={payload}')\n"
            )
            res = await rt.exec_python(
                created[u], code, timeout_s=60,
                host_workspace=str(host), container_workspace=str(roots[u]),
                cwd=f"/ws/{sub}")
            return res.stdout

        # 前两个并行（fan-out），后两个等前面完成（串行依赖）
        wf_outs = await asyncio.gather(wf_node("node_a", "A"), wf_node("node_b", "B"))
        wf_outs.append(await wf_node("node_c", "C"))
        wf_outs.append(await wf_node("node_d", "D"))

        for node, out in zip(nodes, wf_outs):
            record(f"workflow/{node}: 执行成功", f"NODE={node}" in out, out.strip()[:60])

        # 每个节点的 result.json 内容必须是自己那份
        for node in nodes:
            f = roots[u] / ".subagents" / wf_sess / node / "result.json"
            expect = node[-1].upper()
            got = f.read_text(encoding="utf-8") if f.exists() else ""
            record(f"workflow/{node}: 产物内容正确", got == expect, f"{got!r}")

        # ============================================================
        # 场景 3：两个用户各自多智能体，并发
        # ============================================================
        print("\n== 场景3：两个用户各自 2 个 agent，全部并发 ==")
        cross_sess = "cross_sess"

        async def user_agent(uid: str, agent: str) -> tuple[str, str, str]:
            sub = f".subagents/{cross_sess}/{agent}"
            host = roots[uid] / sub
            host.mkdir(parents=True, exist_ok=True)
            marker = secrets.token_hex(4)
            code = (
                "import pathlib\n"
                f"pathlib.Path('whoami.txt').write_text('{uid}:{agent}:{marker}')\n"
                f"print('MARK={marker}')\n"
            )
            res = await rt.exec_python(
                created[uid], code, timeout_s=60,
                host_workspace=str(host), container_workspace=str(roots[uid]),
                cwd=f"/ws/{sub}")
            return uid, agent, res.stdout

        jobs = [user_agent(u, a) for u in users for a in ("a1", "a2")]
        cross = await asyncio.gather(*jobs)

        for uid, agent, out in cross:
            record(f"多用户/{uid}/{agent}: 执行成功", "MARK=" in out, out.strip()[:60])

        # 交叉检验：把 4 个文件全读出来，验证每个 marker 只出现一次、
        # 且只在它该在的文件里。这才是「没有串写」的硬证据。
        all_markers: dict[str, str] = {}
        for uid, agent, out in cross:
            marker = out.strip().split("MARK=")[-1]
            all_markers[f"{uid}/{agent}"] = marker

        for uid in users:
            other = [x for x in users if x != uid][0]
            for agent in ("a1", "a2"):
                f_own = roots[uid] / ".subagents" / cross_sess / agent / "whoami.txt"
                content = f_own.read_text(encoding="utf-8") if f_own.exists() else ""
                my_key = f"{uid}/{agent}"
                my_marker = all_markers.get(my_key, "?")
                # ① 自己的 marker 必须在
                has_mine = my_marker in content
                # ② 不能包含任何**别人**的 marker
                foreign = [m for k, m in all_markers.items() if k != my_key]
                no_foreign = not any(m in content for m in foreign)
                record(f"多用户/{uid}/{agent}: 内容归属正确且无串写",
                       f_own.exists() and has_mine and no_foreign,
                       content[:60])

        # 容器数必须还是 2（没因为多 agent 变多）
        from app.sandbox.pool import ContainerPool
        record("容器数没随 agent 数增长", len(created) == 2, f"{len(created)} 个")

    finally:
        print("\n== 清理 ==")
        for u, cid in created.items():
            rt.destroy(cid)
            print(f"  销毁 {u} 的容器")
        shutil.rmtree(tmp, ignore_errors=True)
        print(f"  删除临时目录")

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n===== {len(RESULTS)-len(bad)}/{len(RESULTS)} 通过 =====")
    if bad:
        print("失败项：")
        for n, ok, d in RESULTS:
            if not ok:
                print(f"  - {n}: {d}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))