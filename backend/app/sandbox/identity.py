"""实例身份：谁在跑、容器归谁。

## 为什么需要

容器只存在于**本机 Docker daemon** 里。单进程时"谁建的"不重要，
但扩容后就变成关键信息：

- 同一台机器跑两个后端进程 -> 都建 `hlz_sbx_<user_id>` 会**名字冲突**；
- 新进程启动时清扫残留 -> 会把**另一个进程正在用**的容器删掉。

## owner_id 的构成

``<hostname>:<pid>``  —— 没注入实例标识时（默认，也是单机开发形态）

``<hostname>:<HLZ_INSTANCE_ID>``  —— 注入了实例标识时（容器/k8s 部署形态）

- 只用 hostname 不够：同机多进程会撞；
- 只用 pid 不够：多机上 pid 会撞（都可能是 1234）；
- 两者组合才能唯一标识"一个运行中的后端实例"。

## 为什么必须有 `HLZ_INSTANCE_ID` 这条路（2026-11-19 修）

容器里 **PID 恒为 1**。只要编排器让所有副本的 hostname 相同
（hostNetwork、StatefulSet 未显式设 hostname、某些 PaaS），
所有副本的 owner 就都是 ``web-0:1`` —— 撞在一起。

撞了之后的后果不是「启动报错」，而是**静默的数据分叉**：

```
副本 A 在跑 g1（DB owner="web-0:1"）
副本 B 收到同一会话的请求
  B: is_generation_dead_elsewhere("g1")
       owner == my_owner -> "是我自己的，交给进程内判断"
       B 的内存里没有 g1 -> 判 stale -> 覆盖记录
  A 还在写 -> 分叉
```

`live_owners()` 也只剩一个成员，容器清扫的保护同时失效。
所以必须有一条「运维显式注入唯一标识」的通路，且它要**真的被用上**
（此前 `cluster.config.instance_id()` 是死代码，`HLZ_INSTANCE_ID` 从未生效）。

## 前缀为什么不能丢

`probe.cleanup_orphans(same_machine=True)` 用
``owner.startswith(f"{owner_machine()}:")`` 判断「本机容器」。
实例标识无论多长多怪，`<机器名>:` 前缀都必须保留，否则启动清扫
再也清不到本机残留（静默泄漏容器）。

注意这**不是**跨机协调机制 —— 容器本来就在各自机器上，
owner 只是让清扫能区分"我的"和"别人的"。真正的跨机协调（如 pool 状态外置）
要走 Redis，那是扩容后的事。

## 为什么不持久化

进程重启后 owner 变了，旧容器就成了"孤儿"——这正是我们要的：
新进程不该复用旧进程的容器（旧容器里的进程可能处于任意状态）。
它们会被下一次清扫按"不是我的"规则跳过，由 TTL 回收或运维手动清。
"""
from __future__ import annotations

import hashlib
import os
import re
import socket

# 标签键（与 probe.SANDBOX_LABEL 同族）
OWNER_LABEL = "hlz.owner"

# owner_id 只允许这些字符，保证能安全用作容器名片段与标签值。
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")

# 机器名上限（沿用原值，容器名有长度限制）
_MACHINE_MAX = 32
# 实例标识上限：机器名 32 + ':' + 标识 ≤ 72，给容器名留余量
# （`runtime.container_name` 会把 owner 再截到 64，所以这里必须先小）
_TAG_MAX = 36


def _hostname() -> str:
    """短主机名。取第一个点之前的部分，避免 FQDN 太长。

    容器名有长度限制，FQDN 可能几十个字符；短名足够区分机器。
    """
    try:
        raw = socket.gethostname() or "unknown"
    except Exception:  # noqa: BLE001 - 取不到主机名不该让服务起不来
        raw = "unknown"
    short = raw.split(".")[0]
    return _SAFE.sub("-", short)[:_MACHINE_MAX] or "unknown"


def _tag_for(raw: str) -> str:
    """把一个原始实例标识净化成安全、够短、**仍唯一**的尾部。

    三个约束：
    - **安全**：tag 要进 Docker 标签值和容器名，只留 `[A-Za-z0-9_.-]`；
    - **够短**：容器名是 ``hlz_sbx_<user>_<owner>``，owner 又被截到 64，
      所以 tag 自己必须先收敛；
    - **仍唯一**：净化/截断绝不能把两个不同标识抹成同一个 ——
      抹平了就是本次要修的那个数据分叉 bug。

    所以「净化改动过」或「发生过截断」时都补一段内容哈希尾巴。
    干净且不长的标识（如 ``pod-0``）原样通过，保持可读。
    """
    safe = _SAFE.sub("-", raw)
    changed = safe != raw
    if len(safe) > _TAG_MAX:
        safe = safe[:_TAG_MAX - 9]
        changed = True
    if changed:
        digest = hashlib.blake2b(raw.encode("utf-8"), digest_size=4).hexdigest()
        safe = f"{safe.rstrip('-')}-{digest}"
    return safe or "unknown"


def _instance_tag() -> str:
    """owner 的尾部：注入了实例标识就用它，否则退回 PID。

    **这里是 `HLZ_INSTANCE_ID` 唯一的消费点。** 在此之前
    `cluster.config.instance_id()` 是死代码，环境变量完全没生效 ——
    容器里 PID 恒为 1，所有副本 owner 撞在一起，跨实例判活随机失效。
    """
    raw = (os.environ.get("HLZ_INSTANCE_ID") or "").strip()
    if raw:
        return _tag_for(raw)
    return str(os.getpid())


def owner_id() -> str:
    """本进程的归属标识：``<hostname>:<实例标识或 PID>``。

    同一个进程内恒定，不同进程/不同机器不同（前提是实例标识真的唯一，
    见 `cluster.instances` 里的冲突检测）。

    **`<机器名>:` 前缀必须保留** —— `probe.cleanup_orphans` 靠它判断本机容器。
    """
    return f"{_hostname()}:{_instance_tag()}"


def owner_machine() -> str:
    """本机标识（不含 PID/实例标识）。用于「清掉本机所有我这类容器」的场景。"""
    return _hostname()