"""数据库后端选择：读 `HLZ_DB_URL` 决定走 SQLite 还是 PostgreSQL。

## 为什么要单独一个模块

`sqlcompat.connect()` 需要知道后端，但 `sqlcompat` 又被 `sqlitedriver` /
`pgdriver` import —— 放在同一个模块里会循环 import。这个模块**不 import 任何
驱动**，只做「读环境变量 + 缓存」。

## 配置

    HLZ_DB_URL 缺省 / 空                 -> SQLite，用各模块传进来的 DB_PATH
    HLZ_DB_URL=sqlite:///D:/x/app.db     -> SQLite，用 URL 里的路径
    HLZ_DB_URL=postgresql://u:p@h:5432/d -> PostgreSQL

## 为什么缓存

`connect()` 每次数据库操作都会调一次，126 处调用点意味着**每秒几十次**。
读环境变量本身不贵，但每次构造 / 校验 URL 是浪费。缓存后只在首次解析。
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_POSTGRES_SCHEMES = ("postgres://", "postgresql://", "postgres+asyncpg://",
                     "postgresql+asyncpg://")


@dataclass(frozen=True)
class DbTarget:
    backend: str                 # "sqlite" | "postgres"
    sqlite_path: Optional[str] = None
    pg_dsn: Optional[str] = None

    @property
    def is_postgres(self) -> bool:
        return self.backend == "postgres"


_cached: Optional[DbTarget] = None
_cached_raw: Optional[str] = None


def _parse(raw: str) -> DbTarget:
    url = (raw or "").strip()
    if not url:
        return DbTarget(backend="sqlite")
    lowered = url.lower()
    if lowered.startswith(_POSTGRES_SCHEMES):
        # asyncpg 只认 postgresql://；把 SQLAlchemy 风格的 +asyncpg 去掉
        dsn = url
        for prefix in ("postgres+asyncpg://", "postgresql+asyncpg://"):
            if lowered.startswith(prefix):
                dsn = "postgresql://" + url[len(prefix):]
                break
        if lowered.startswith("postgres://"):
            dsn = "postgresql://" + url[len("postgres://"):]
        return DbTarget(backend="postgres", pg_dsn=dsn)
    if lowered.startswith("sqlite:///"):
        return DbTarget(backend="sqlite", sqlite_path=url[len("sqlite:///"):])
    if lowered.startswith("sqlite://"):
        return DbTarget(backend="sqlite", sqlite_path=url[len("sqlite://"):])
    # 裸路径也当 sqlite（方便本地试验）
    return DbTarget(backend="sqlite", sqlite_path=url)


def target(*, reload: bool = False) -> DbTarget:
    """当前后端目标（带缓存）。"""
    global _cached, _cached_raw
    raw = os.environ.get("HLZ_DB_URL", "")
    if _cached is not None and not reload and _cached_raw == raw:
        return _cached
    _cached = _parse(raw)
    _cached_raw = raw
    return _cached


def is_postgres() -> bool:
    return target().is_postgres


def resolve_sqlite_path(default) -> Path:
    """SQLite 后端该用哪个文件：URL 里指定的优先，否则用传进来的默认值。"""
    t = target()
    if t.sqlite_path:
        return Path(t.sqlite_path)
    return Path(default)


def pg_dsn() -> str:
    t = target()
    if not t.pg_dsn:
        raise RuntimeError("HLZ_DB_URL 未指向 PostgreSQL")
    return t.pg_dsn


def reset_for_tests() -> None:
    """测试用：清缓存，让下次 target() 重新读环境变量。"""
    global _cached, _cached_raw
    _cached = None
    _cached_raw = None


# ---------------------------------------------------------------- PG schema 映射
#
# ## 为什么需要这个
#
# 测试用 `monkeypatch DB_PATH -> tmp_path/app.db` 实现隔离，每个用例一个
# 独立 SQLite 文件。迁到 PG 后「文件」这个概念没了 —— 如果所有用例共享
# 一个 PG 数据库，`isolated_db` 立刻互相污染，1269 条测试根本跑不起来。
#
# 解法：把 `DB_PATH` 的**路径字符串**映射成 PG 里的一个 schema。
# 同一个路径 -> 同一个 schema；不同 tmp 路径 -> 不同 schema。
# 生产下 `HLZ_PG_SCHEMA_PER_PATH` 不设，永远是 `public`，行为不变。
#
# 这样 `connect(path)` 的签名一个字不用改，测试隔离自动成立。

import hashlib
import re as _re

_SAFE_SCHEMA = _re.compile(r"[^a-z0-9_]")


def schema_per_path_enabled() -> bool:
    return os.environ.get("HLZ_PG_SCHEMA_PER_PATH", "") not in ("", "0", "false", "False")


def pg_schema_for(path) -> str:
    """`path` 对应的 PG schema 名；生产下恒为 `public`。"""
    if not schema_per_path_enabled():
        return "public"
    raw = str(path or "default")
    # Windows 路径大小写不敏感，统一小写再哈希，避免同一目录算出两个 schema
    digest = hashlib.sha1(raw.replace("\\", "/").lower().encode("utf-8")).hexdigest()
    return "hlz_" + _SAFE_SCHEMA.sub("", digest[:16])