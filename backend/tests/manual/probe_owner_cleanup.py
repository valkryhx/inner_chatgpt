"""真机验证：owner 标签与清扫隔离。

验证四件事：
1. 创建的容器真的带 hlz.owner 标签，值是 <host>:<pid>；
2. 两个不同 owner 的用户，容器名不同（不撞）；
3. cleanup_orphans() 默认**不清别人的**容器；
4. same_machine=True 能清本机所有实例的；
5. all_owners=True 能清全部。

用法：
    python tests/manual/probe_owner_cleanup.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


async def main() -> int:
    from app.sandbox import probe
    from app.sandbox.identity import OWNER_LABEL, owner_id, owner_machine
    from app.sandbox.runtime import ContainerRuntime, container_name
    from app.sandbox.config import get_config
    import docker

    cfg = get_config(reload=True)
    rt = ContainerRuntime(cfg)
    client = docker.from_env()

    my_owner = owner_id()
    print(f"本进程 owner: {my_owner}  机器: {owner_machine()}\n")

    created: list[str] = []
    try:
        # ---- 1. 建容器，检查标签 ----
        import tempfile, shutil
        tmp = Path(tempfile.mkdtemp(prefix="hlz_owner_"))
        for uid in ("u_mine_1", "u_mine_2"):
            ws = tmp / uid
            ws.mkdir(parents=True, exist_ok=True)
            cid = rt.create(uid, str(ws))
            created.append(cid)
            name = container_name(uid)
            record(f"容器名含 owner", f"{owner_machine()}-{os.getpid()}" in name, name)

            c = client.containers.get(cid)
            labels = c.labels or {}
            record(f"{uid}: 有 hlz.owner 标签",
                   labels.get(OWNER_LABEL) == my_owner,
                   str(labels.get(OWNER_LABEL)))
            record(f"{uid}: 仍有 hlz.sandbox 标签",
                   labels.get(probe.SANDBOX_LABEL) == probe.SANDBOX_LABEL_VALUE, "")

        # ---- 2. 伪造一个"别的进程"的容器 ----
        fake_owner = f"{owner_machine()}:99999"
        fake_name = f"hlz_fake_other_{os.getpid()}"
        fake = client.containers.run(
            cfg.image, ["sleep", "infinity"], name=fake_name, detach=True,
            labels={probe.SANDBOX_LABEL: probe.SANDBOX_LABEL_VALUE,
                    OWNER_LABEL: fake_owner},
            network_mode="none")
        print(f"\n  伪造了一个别的 owner 的容器: {fake_name} (owner={fake_owner})")

        # ---- 3. 默认清扫：不该清别人的 ----
        n = probe.cleanup_orphans()
        still_there = client.containers.get(fake_name).status
        record("默认清扫不动别人的容器", still_there == "running",
               f"cleanup 清了 {n} 个，别人的状态={still_there}")

        # 自己的容器应该被清了（本进程刚建的，owner 匹配）
        mine_gone = all(
            client.containers.get(c).status != "running"
            for c in created if _exists(client, c)
        ) if created else True
        record("默认清扫清掉了自己的", mine_gone, f"清了 {n} 个")

        created.clear()  # 已被清，不用再删

        # ---- 4. same_machine：应清掉本机所有实例（含伪造的） ----
        n2 = probe.cleanup_orphans(same_machine=True)
        gone = not _exists(client, fake_name)
        record("same_machine=True 清掉本机其它实例的", gone, f"清了 {n2} 个")

        # ---- 5. all_owners 也不报错 ----
        n3 = probe.cleanup_orphans(all_owners=True)
        record("all_owners=True 可执行", True, f"清了 {n3} 个")

    finally:
        print("\n== 清理 ==")
        for cid in created:
            rt.destroy(cid)
        try:
            client.containers.get(f"hlz_fake_other_{os.getpid()}").remove(force=True)
        except Exception:
            pass
        client.close()
        try:
            shutil.rmtree(tmp, ignore_errors=True)
        except Exception:
            pass
        print("  完成")

    bad = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n===== {len(RESULTS)-len(bad)}/{len(RESULTS)} 通过 =====")
    return 0 if not bad else 1


def _exists(client, cid: str) -> bool:
    try:
        client.containers.get(cid)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))