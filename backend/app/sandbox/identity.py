"""实例身份：谁在跑、容器归谁。

## 为什么需要

容器只存在于**本机 Docker daemon** 里。单进程时"谁建的"不重要，
但扩容后就变成关键信息：

- 同一台机器跑两个后端进程 -> 都建 `hlz_sbx_<user_id>` 会**名字冲突**；
- 新进程启动时清扫残留 -> 会把**另一个进程正在用**的容器删掉。

## owner_id 的构成

``<hostname>:<pid>``

- 只用 hostname 不够：同机多进程会撞；
- 只用 pid 不够：多机上 pid 会撞（都可能是 1234）；
- 两者组合才能唯一标识"一个运行中的后端实例"。

注意这**不是**跨机协调机制 —— 容器本来就在各自机器上，
owner 只是让清扫能区分"我的"和"别人的"。真正的跨机协调（如 pool 状态外置）
要走 Redis，那是扩容后的事。

## 为什么不持久化

进程重启后 owner 变了，旧容器就成了"孤儿"——这正是我们要的：
新进程不该复用旧进程的容器（旧容器里的进程可能处于任意状态）。
它们会被下一次清扫按"不是我的"规则跳过，由 TTL 回收或运维手动清。
"""
from __future__ import annotations

import os
import re
import socket

# 标签键（与 probe.SANDBOX_LABEL 同族）
OWNER_LABEL = "hlz.owner"

# owner_id 只允许这些字符，保证能安全用作容器名片段与标签值。
_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def _hostname() -> str:
    """短主机名。取第一个点之前的部分，避免 FQDN 太长。

    容器名有长度限制，FQDN 可能几十个字符；短名足够区分机器。
    """
    try:
        raw = socket.gethostname() or "unknown"
    except Exception:  # noqa: BLE001 - 取不到主机名不该让服务起不来
        raw = "unknown"
    short = raw.split(".")[0]
    return _SAFE.sub("-", short)[:32] or "unknown"


def owner_id() -> str:
    """本进程的归属标识：``<hostname>:<pid>``。

    同一个进程内恒定，不同进程/不同机器不同。
    """
    return f"{_hostname()}:{os.getpid()}"


def owner_machine() -> str:
    """本机标识（不含 PID）。用于「清掉本机所有我这类容器」的场景。"""
    return _hostname()