"""每用户记忆库：文件即真相 + SQLite 索引。

设计对齐 ``skill_store.py``（同一套目录/路径安全写法），并照
``docs/20261112-Hlaudz记忆系统实施方案.md`` §2 / §3.1 / §5.1 落地：

    正文永远在 .md 文件里
        backend/data/memories/<user_id>/private/<slug>.md
    DB 只做索引（列举 / 排序 / 命中统计 / 审计），永远不是正文来源

为什么这样分：
  1. 与 CC 一致 —— 记忆是 markdown，模型能直接用 file_read / file_patch 改；
  2. 用户在设置页手改文件后不会出现「库里有、文件没了」的分裂；
  3. 单条记忆可能几千字，塞进 SQLite TEXT 会让 session_history 那套读写变重。

三条隔离保证（§2.3）：
  - 路径：所有落点都过 ``workspace._resolve_under``，写不进别人的目录；
  - 查询：每个公开函数的第一个参数就是 ``user_id``，SQL 里 ``WHERE user_id=?`` 是硬条件；
  - 注入：注入器必须闭包捕获 ``user_id``（见 memory_inject），不读请求参数。

对外 API（全部 async：DB 走 aiosqlite，与 subagent_store / team_store 一致）::

    await init_tables()
    memory_root(user_id, create=...)
    await list_memories(user_id, limit=50, cursor=None) -> MemoryPage
    await get_memory(user_id, slug) -> MemorySpec | None
    await write_memory(user_id, slug=..., title=..., description=..., body=...,
                       if_version=...) -> WriteResult
    await delete_memory(user_id, slug) -> bool
    await select_for_injection(user_id, query_hint=..., char_budget=...) -> list[MemorySpec]
    format_memory_block(specs, char_budget, withheld=0) -> str
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH, MEMORIES_DIR
from .workspace import PathSecurityError, _reject_unsafe_segment, _resolve_under

logger = logging.getLogger(__name__)

# ---------------- 常量 ----------------

SCOPE_PRIVATE = "private"
SCOPE_TEAM = "team"
SCOPES = (SCOPE_PRIVATE, SCOPE_TEAM)

# CC 的四类记忆（v3 §5.7）。kind 影响注入排序与提示词，不影响物理落点。
KIND_USER = "user"
KIND_FEEDBACK = "feedback"
KIND_PROJECT = "project"
KIND_REFERENCE = "reference"
KINDS = (KIND_USER, KIND_FEEDBACK, KIND_PROJECT, KIND_REFERENCE)

# 注入优先级：user 最前（承载称呼/语气，每轮都影响表达），
# project 最后（有效期最短、最容易过时）。§3.2
KIND_PRIORITY: dict[str, int] = {
    KIND_USER: 0,
    KIND_FEEDBACK: 1,
    KIND_REFERENCE: 2,
    KIND_PROJECT: 3,
}

# 入口索引文件名。不可作为记忆 slug（避免自我引用）。
INDEX_NAME = "MEMORY.md"

# 单条记忆正文上限（对齐 CC 的 100 KB，§5.1）。
MAX_MEMORY_BYTES = 100 * 1024

# 默认页大小（对齐 CC 的 Hi = 50）。
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200

# slug 规则与 skill 名一致：小写字母数字开头，只含 [-_.]，1..64。
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_KV_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*:\s*(.*)$")
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)

# ---------------- 配置（环境变量，全部有默认可跑） ----------------


def _env_flag(name: str, default: bool) -> bool:
    raw = (os.environ.get(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, *, lo: int = 0) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(lo, int(raw))
    except (TypeError, ValueError):
        return default


def memory_enabled() -> bool:
    """总开关。0 = 注入器返回空、工具不注册（等价于整套功能关闭）。"""
    return _env_flag("HLZ_MEMORY_ENABLED", True)


def memory_hard_disabled() -> bool:
    """排障出口：1 = 自动提取与 dream 都不跑（模型显式调用仍可用）。

    对齐 CC 的 ``settingsHooksOff``。与 ``memory_enabled`` 的区别：
    后者连读侧注入一起关，本开关只关「自己长东西」的两条后台路径。
    """
    return _env_flag("HLZ_MEMORY_DISABLED", False)


def inject_budget() -> int:
    """每轮注入的字符预算（code point，§3.6）。"""
    return _env_int("HLZ_MEMORY_INJECT_BUDGET", 2000, lo=200)


def inject_max_entries() -> int:
    return _env_int("HLZ_MEMORY_INJECT_MAX", 12, lo=1)


def auto_write_enabled() -> bool:
    """自动提取总开关。**默认 0（关闭）**。

    它是唯一会「自己长东西」的机制：读侧做错最坏是没注入记忆，
    写侧做错会往库里塞错东西，而且**下一个会话会把它当事实注入**——
    错误单向累积且自我强化。CC 自己的 ``tengu_passport_quail`` 代码默认也是
    ``false``（核实时序文档 §4），默认行为对齐它是最稳的选择。

    反过来 ``memory_write`` **工具**默认是开的：它不花额外钱、错了是当轮
    明确决定、能在对话里看见工具步骤，用户能立刻纠正。**可见性**是这个
    开关和自动提取最大的区别。
    """
    return _env_flag("HLZ_MEMORY_AUTO_WRITE", False)


def dream_enabled() -> bool:
    """``auto_dream`` 合并总开关。默认 1，但**上线顺序上要 M4 之后才打开**：
    没有提取产生的碎片时它无事可做（见方案 §9 的上线顺序）。
    """
    return _env_flag("HLZ_MEMORY_DREAM_ENABLED", True)


def index_max_chars() -> int:
    """索引 cap，单位是 **code point / UTF-16 code unit**，不是字节（§3.6）。

    按字节实现会让中文索引在 1/3 处被截 —— 见 v3 §1.3。
    """
    return _env_int("HLZ_MEMORY_INDEX_MAX_CHARS", 25000, lo=1000)


def index_max_lines() -> int:
    return _env_int("HLZ_MEMORY_INDEX_MAX_LINES", 200, lo=10)


def recall_max_bytes() -> int:
    """单文件召回上限，单位是**真字节**（与索引口径不同，§3.6）。"""
    return _env_int("HLZ_MEMORY_RECALL_MAX_BYTES", 4096, lo=256)


# ---------------- 数据结构 ----------------


@dataclass
class MemorySpec:
    """一条记忆。``body`` 只在读取单条时填充，列举走索引不读正文。"""

    slug: str
    title: str
    description: str
    kind: str = KIND_USER
    tags: tuple[str, ...] = ()
    scope: str = SCOPE_PRIVATE
    team_key: str = ""
    body: str = ""
    path: Optional[Path] = None
    created_at: int = 0
    updated_at: int = 0
    version: str = ""
    # 正文长度（按字符计）。列举不读正文，但要能在列表里显示「多少字」，
    # 所以解析文件时顺手记一下 —— 比让前端为每条再发一次请求便宜得多。
    body_chars: int = 0

    @property
    def label(self) -> str:
        """注入块里显示的名字：优先 title，退回 slug。"""
        return (self.title or self.slug).strip()

    @property
    def chars(self) -> int:
        """正文长度。读过正文就用正文算，没读过用解析时的快照。"""
        return len(self.body) if self.body else self.body_chars

    def to_public_dict(self) -> dict[str, Any]:
        """给工具/API 用的形状（不含正文，正文走 memory_read）。

        ``version`` 必须带上：模型看完 ``memory_list`` 后要更新某条时，
        手里得有 if_version 才能做 CAS（§5.1）。
        """
        return {
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "kind": self.kind,
            "tags": list(self.tags),
            "scope": self.scope,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
            "chars": self.chars,
        }


@dataclass
class MemoryPage:
    """``list_memories`` 的返回：游标分页（对齐 CC 的 entries / remaining）。"""

    entries: list[MemorySpec] = field(default_factory=list)
    remaining: int = 0
    next_cursor: str = ""
    total: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "entries": [m.to_public_dict() for m in self.entries],
            "remaining": self.remaining,
            "next_cursor": self.next_cursor,
            "total": self.total,
        }


@dataclass
class WriteResult:
    """``write_memory`` 的结果。

    ``outcome`` 对齐 CC：``ok`` / ``conflict`` / ``missing`` / ``refused`` / ``failed``。
    冲突时带 ``current_version`` + ``current_content``，调用方据此重放。
    """

    outcome: str
    spec: Optional[MemorySpec] = None
    current_version: str = ""
    current_content: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "ok"


# ---------------- 路径 ----------------


def memory_root(user_id: str, *, create: bool = False) -> Path:
    """``backend/data/memories/<user_id>``。越界片段直接拒绝。"""
    _reject_unsafe_segment(user_id)
    root = MEMORIES_DIR / user_id
    if create:
        root.mkdir(parents=True, exist_ok=True)
    return root


def scope_dir(user_id: str, scope: str = SCOPE_PRIVATE, *, create: bool = False) -> Path:
    """``.../<user_id>/<scope>[/<team_key>]``。

    第一版只有 private：``team`` 作用域会抛 ``ValueError``。
    这样做而不是静默回落，是为了让「想用 team 的人」立刻知道还没实现，
    而不是写了三条记忆发现它们悄悄进了私有目录。
    """
    scope = (scope or SCOPE_PRIVATE).strip().lower()
    if scope not in SCOPES:
        raise ValueError(f"未知作用域: {scope}（可选 {SCOPES}）")
    if scope == SCOPE_TEAM:
        raise ValueError("team 作用域尚未实现（第一版只有 private）")
    path = memory_root(user_id, create=create) / scope
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def memory_path(user_id: str, slug: str, *, scope: str = SCOPE_PRIVATE,
                create_dir: bool = False) -> Path:
    """一条记忆的文件路径，且保证不越出该用户的作用域目录。

    ``create_dir=False``（默认）只计算路径，不落盘也不建目录 —— 读路径
    不该因为「查一条不存在的记忆」就在磁盘上长出一个空目录。
    """
    slug = validate_slug(slug)
    base = scope_dir(user_id, scope, create=create_dir)
    return _resolve_under(base, base / f"{slug}.md")


def index_path(user_id: str, *, scope: str = SCOPE_PRIVATE) -> Path:
    return scope_dir(user_id, scope, create=True) / INDEX_NAME


# ---------------- slug / 解析 ----------------


def validate_slug(slug: str) -> str:
    """校验并归一 slug。非法输入抛 ``ValueError``（工具层会转成可读错误）。"""
    s = (slug or "").strip().lower()
    if s.endswith(".md"):
        s = s[:-4]
    if not s:
        raise ValueError("slug 不能为空")
    if s == INDEX_NAME.lower()[:-3]:  # 'memory'
        raise ValueError(f"slug 不能是保留名 {INDEX_NAME}")
    if not _SLUG_RE.match(s):
        raise ValueError(
            "slug 只能包含小写字母、数字、'-'、'_'、'.'，以字母或数字开头，长度 1-64"
        )
    if ".." in s:
        raise ValueError("slug 不能包含 '..'")
    return s


def slugify(text: str) -> str:
    """从任意文本派生一个合法 slug（中文等非 ASCII 变成 '-'）。"""
    raw = (text or "").strip().lower()
    if raw.endswith(".md"):
        raw = raw[:-4]
    slug = re.sub(r"[^a-z0-9._-]+", "-", raw).strip("-._")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug or not re.match(r"^[a-z0-9]", slug):
        slug = f"memory-{secrets.token_hex(4)}"
    return slug[:64]


def _scalar(value: str) -> Any:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        inner = v[1:-1]
        if v[0] == '"':
            inner = inner.replace('\\"', '"').replace("\\\\", "\\")
        return inner
    if v.startswith("[") and v.endswith("]"):
        return [p.strip() for p in v[1:-1].split(",") if p.strip()]
    return v


def parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """解析记忆文件：``(frontmatter dict, body)``。

    只支持记忆用得到的 YAML 子集：``key: value``、行内 ``[a, b]``、
    以及一层缩进块（``metadata:`` 下的 ``type:`` / ``tags:`` 列表）。
    不引第三方 yaml 依赖 —— 记忆的 frontmatter 是我们自己渲染的，
    形状可控，不需要完整 YAML。

    嵌套深度**故意只有两层**：再深就会出现「渲染器写得出、解析器读不回」
    的单向兼容，那才是真正的坑。外层 key 与子 key 都是平铺进 dict 的
    （``metadata.type`` 记成 ``"metadata" -> {...}``）。
    """
    m = _FRONTMATTER_RE.match(text or "")
    if not m:
        return {}, (text or "")
    fm, body = m.group(1), (text or "")[m.end():]
    lines = fm.splitlines()
    out: dict[str, Any] = {}
    i = 0
    while i < len(lines):
        raw = lines[i]
        if not raw.strip() or raw.lstrip().startswith("#"):
            i += 1
            continue
        kv = _KV_RE.match(raw)
        if not kv:
            i += 1
            continue
        key = kv.group(1).replace("-", "_")
        val = kv.group(2).strip()
        i += 1
        if val:
            out[key] = _scalar(val)
            continue
        # 空值 -> 读一层缩进块：可能是 list（`- x`），也可能是 dict。
        block = _read_indented_block(lines, i)
        i = block.next_index
        out[key] = block.value
    return out, body


@dataclass
class _Block:
    value: Any
    next_index: int


def _read_indented_block(lines: list[str], start: int) -> _Block:
    """从 ``start`` 开始读一层缩进内容，返回 (值, 下一行索引)。

    缩进基准取块内第一条非空行的缩进，遇到缩进不足的非空行即结束。
    """
    items: list[str] = []
    mapping: dict[str, Any] = {}
    base_indent: Optional[int] = None
    i = start
    while i < len(lines):
        raw = lines[i]
        if not raw.strip():
            i += 1
            continue
        indent = _indent_of(raw)
        if base_indent is None:
            if indent == 0:
                break
            base_indent = indent
        if indent < base_indent:
            break
        s = raw.strip()
        i += 1
        if s.startswith("- "):
            items.append(str(_scalar(s[2:])))
            continue
        kv = _KV_RE.match(s)
        if not kv:
            continue
        k = kv.group(1).replace("-", "_")
        v = kv.group(2).strip()
        if v:
            mapping[k] = _scalar(v)
            continue
        # 子 key 也是空值：只再读一层 list（tags 列表就是这种形状）
        sub = _read_indented_block(lines, i)
        i = sub.next_index
        mapping[k] = sub.value
    return _Block(items if items else mapping, i)


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _as_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(x).strip() for x in value if str(x).strip()]
    if isinstance(value, str):
        return [p.strip() for p in re.split(r"[,;]", value) if p.strip()]
    return []


def _one_line(value: str) -> str:
    """frontmatter 的标量字段必须单行 —— 换行会让「key: value」变成两块。"""
    return " ".join(str(value or "").split())


def render_memory(spec: MemorySpec) -> str:
    """把一条记忆渲染成 .md 文本（frontmatter + 正文）。

    形状故意保持 CC 的样子（``metadata.type`` / ``metadata.tags``），
    这样从 CC 迁移过来的记忆文件可以直接读。多一个 ``title`` 字段：
    CC 的 ``name`` 在 Hlaudz 里必须是文件名（改名要靠写新文件），
    所以显示名另存，不能复用 name。
    """
    lines = ["---", f"name: {spec.slug}"]
    if spec.title:
        lines.append(f"title: {_one_line(spec.title)}")
    lines.append(f"description: {_one_line(spec.description)}")
    lines.append("metadata:")
    lines.append(f"  type: {spec.kind}")
    tags = [_one_line(t) for t in spec.tags if _one_line(t)]
    if tags:
        lines.append("  tags:")
        for t in tags:
            lines.append(f"    - {t}")
    else:
        lines.append("  tags: []")
    if spec.created_at:
        lines.append(f"created: {_iso_day(spec.created_at)}")
    lines.append("---")
    body = (spec.body or "").rstrip("\n")
    return "\n".join(lines) + "\n" + body + ("\n" if body else "")


def _iso_day(ts_ms: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000))
    except Exception:  # noqa: BLE001
        return ""


def _parse_memory_file(path: Path, *, scope: str = SCOPE_PRIVATE,
                       team_key: str = "", with_body: bool = False) -> Optional[MemorySpec]:
    """把一个 .md 读成 MemorySpec。解析失败返回 None（坏文件不该炸整次列举）。"""
    try:
        text = path.read_text(encoding="utf-8")
        stat = path.stat()
    except (OSError, UnicodeDecodeError):
        return None
    meta, body = parse_frontmatter(text)
    slug = str(meta.get("name") or path.stem).strip().lower()
    if slug.endswith(".md"):
        slug = slug[:-4]
    if not _SLUG_RE.match(slug):
        slug = slugify(slug or path.stem)
    nested = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    kind = str(meta.get("type") or nested.get("type") or KIND_USER).strip().lower()
    if kind not in KINDS:
        kind = KIND_USER
    desc = str(meta.get("description") or "").strip()
    if "description" not in meta:
        # 只有「压根没写这个字段」才回落到正文首行 —— 那是手写文件的常见形态
        # （用户丢一个 .md 进来，没有 frontmatter）。**写了空值**表示作者明确
        # 表示这条没有描述（设置页清空描述就是这种情况），不能悄悄补一个。
        desc = _first_line(body)[:250]
    created_ms = int(stat.st_mtime * 1000)
    created_day = str(meta.get("created") or "").strip()
    if created_day:
        try:
            created_ms = int(time.mktime(time.strptime(created_day, "%Y-%m-%d")) * 1000)
        except (TypeError, ValueError):
            pass
    return MemorySpec(
        slug=slug,
        title=str(meta.get("title") or "").strip() or desc[:80] or slug,
        description=desc,
        kind=kind,
        tags=tuple(_as_list(meta.get("tags") or nested.get("tags"))),
        scope=scope,
        team_key=team_key,
        body=(body or "") if with_body else "",
        path=path,
        created_at=created_ms,
        updated_at=int(stat.st_mtime * 1000),
        version=hashlib.sha256(text.encode("utf-8")).hexdigest()[:12],
        body_chars=len(body or ""),
    )


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        s = line.strip().lstrip("#").strip()
        if s:
            return s
    return ""


# ---------------- DB ----------------


_CREATE_TABLES = """
CREATE TABLE IF NOT EXISTS memories (
    id          TEXT PRIMARY KEY,
    user_id     TEXT NOT NULL,
    scope       TEXT NOT NULL DEFAULT 'private',
    team_key    TEXT NOT NULL DEFAULT '',
    slug        TEXT NOT NULL,
    title       TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags        TEXT NOT NULL DEFAULT '[]',
    kind        TEXT NOT NULL DEFAULT 'user',
    path        TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL,
    hit_count   INTEGER NOT NULL DEFAULT 0,
    last_hit_at INTEGER NOT NULL DEFAULT 0,
    UNIQUE (user_id, scope, team_key, slug)
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories (user_id, scope, updated_at DESC);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


async def init_tables() -> None:
    """幂等建表。启动时调用一次。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.execute("PRAGMA busy_timeout=5000;")
        await conn.executescript(_CREATE_TABLES)
        await conn.commit()


async def _fetch(sql: str, params: tuple = ()) -> list[aiosqlite.Row]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(sql, params)
        return list(await cur.fetchall())


async def _execute(sql: str, params: tuple = ()) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA busy_timeout=5000;")
        cur = await conn.execute(sql, params)
        await conn.commit()
        return cur.rowcount


def _row_to_spec(row: aiosqlite.Row, *, scope: str, team_key: str) -> MemorySpec:
    """索引行 -> MemorySpec。

    ``version`` 现算（sha256 文件内容前 12 位），不缓存在 DB 里：
    版本是 CAS 的比较基准，缓存的版本一旦落后就会让「明明没别人改」的写入
    被误判成冲突。文件是真相，版本也只能从文件算。
    """
    try:
        tags = tuple(json.loads(row["tags"] or "[]"))
    except Exception:  # noqa: BLE001
        tags = ()
    path = Path(str(row["path"]))
    version, _text = _read_version(path)
    return MemorySpec(
        slug=str(row["slug"]),
        title=str(row["title"] or ""),
        description=str(row["description"] or ""),
        kind=str(row["kind"] or KIND_USER),
        tags=tags,
        scope=scope,
        team_key=team_key,
        path=path,
        created_at=int(row["created_at"] or 0),
        updated_at=int(row["updated_at"] or 0),
        version=version,
    )


async def _reconcile(user_id: str, scope: str = SCOPE_PRIVATE, team_key: str = "") -> int:
    """把目录与索引对齐，返回本次修复的条数。

    为什么必须有这一步：方案里明确支持「用户手动加一个 .md」（§8 的最小可用集）。
    只信 DB 会让手写的记忆永远不可见；只读目录会让排序/统计失去意义。
    所以列举前先扫一遍目录，以「文件 mtime」作为版本依据做增量 upsert。

    记忆条数是「几十条」量级，一次 scandir + 几次 stat 的代价可以忽略。
    """
    base = scope_dir(user_id, scope)  # team 作用域会在这一行抛错
    if not base.is_dir():
        return 0
    seen: dict[str, MemorySpec] = {}
    for p in base.glob("*.md"):
        if p.name == INDEX_NAME:
            continue
        spec = _parse_memory_file(p, scope=scope, team_key=team_key)
        if spec is not None:
            seen[spec.slug] = spec

    rows = await _fetch(
        "SELECT slug, updated_at, path FROM memories WHERE user_id=? AND scope=? AND team_key=?",
        (user_id, scope, team_key),
    )
    known = {str(r["slug"]): (int(r["updated_at"] or 0), str(r["path"])) for r in rows}

    fixed = 0
    stale = [s for s, (mtime, _p) in known.items() if s not in seen]
    for slug in stale:
        await _execute(
            "DELETE FROM memories WHERE user_id=? AND scope=? AND team_key=? AND slug=?",
            (user_id, scope, team_key, slug),
        )
        fixed += 1

    for slug, spec in seen.items():
        prev_mtime = known.get(slug, (0, ""))[0]
        if prev_mtime == spec.updated_at and slug in known:
            continue
        await _upsert_row(user_id, spec)
        fixed += 1
    return fixed


async def _upsert_row(user_id: str, spec: MemorySpec) -> None:
    await _execute(
        "INSERT INTO memories(id,user_id,scope,team_key,slug,title,description,tags,kind,path,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(user_id,scope,team_key,slug) DO UPDATE SET "
        "title=excluded.title, description=excluded.description, tags=excluded.tags, "
        "kind=excluded.kind, path=excluded.path, updated_at=excluded.updated_at",
        (
            f"mem_{secrets.token_hex(6)}",
            user_id,
            spec.scope,
            spec.team_key,
            spec.slug,
            spec.title,
            spec.description,
            json.dumps(list(spec.tags), ensure_ascii=False),
            spec.kind,
            str(spec.path or ""),
            spec.created_at or now_ms(),
            spec.updated_at or now_ms(),
        ),
    )


# ---------------- 读 ----------------


async def list_memories(user_id: str, *, scope: str = SCOPE_PRIVATE,
                        limit: int = DEFAULT_PAGE_SIZE,
                        cursor: str | None = None,
                        query: str = "") -> MemoryPage:
    """按 slug 升序分页列举（cursor = 上一页最后一条的 slug）。

    为什么按 slug 而不是 updated_at 排序：``cursor`` 要能稳定续读。按时间排序时
    两次调用之间只要有写入，游标就会漂移，翻页会漏条目或重复条目 ——
    CC 的 cursor 语义也是「上一次返回的最后一个 path」。
    需要「最近更新优先」的调用方（设置页）自己在前端排序。
    """
    await _reconcile(user_id, scope)
    limit = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
    after = ""
    if cursor:
        try:
            after = validate_slug(cursor)
        except ValueError:
            after = ""  # 坏游标按「从头开始」，不抛错（M-11）
    if query.strip():
        # 有关键词时不能走 SQL LIMIT —— 过滤在内存里做，分页会漏条目。
        # 记忆条数是几十量级，全取再过滤是安全的。
        rows = await _fetch(
            "SELECT * FROM memories WHERE user_id=? AND scope=? ORDER BY slug ASC",
            (user_id, scope),
        )
        all_specs = [_row_to_spec(r, scope=scope, team_key="") for r in rows]
        filtered = _filter_by_query(all_specs, query)
        start = 0
        if after:
            start = next((i + 1 for i, m in enumerate(filtered) if m.slug == after), len(filtered))
        window = filtered[start:start + limit]
        remaining = max(0, len(filtered) - start - len(window))
        return MemoryPage(
            entries=window,
            remaining=remaining,
            next_cursor=(window[-1].slug if window and remaining > 0 else ""),
            total=len(filtered),
        )

    rows = await _fetch(
        "SELECT * FROM memories WHERE user_id=? AND scope=? AND slug>? ORDER BY slug ASC LIMIT ?",
        (user_id, scope, after, limit + 1),
    )
    entries = [_row_to_spec(r, scope=scope, team_key="") for r in rows[:limit]]
    has_more = len(rows) > limit
    total_rows = await _fetch(
        "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND scope=?",
        (user_id, scope),
    )
    total = int(total_rows[0]["n"]) if total_rows else 0
    remaining = 0
    if has_more and entries:
        tail = await _fetch(
            "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND scope=? AND slug>?",
            (user_id, scope, entries[-1].slug),
        )
        remaining = int(tail[0]["n"]) if tail else 0
    return MemoryPage(
        entries=entries,
        remaining=remaining,
        next_cursor=(entries[-1].slug if entries and remaining > 0 else ""),
        total=total,
    )


def _filter_by_query(entries: list[MemorySpec], query: str) -> list[MemorySpec]:
    q = query.strip().lower()
    if not q:
        return entries
    return [
        m for m in entries
        if q in m.slug.lower() or q in m.title.lower() or q in m.description.lower()
        or any(q in t.lower() for t in m.tags)
    ]


async def get_memory(user_id: str, slug: str, *, scope: str = SCOPE_PRIVATE,
                     with_body: bool = True) -> Optional[MemorySpec]:
    """读一条记忆。文件是唯一真相 —— 不查 DB 直接读文件。"""
    try:
        path = memory_path(user_id, slug, scope=scope)
    except ValueError:
        return None
    if not path.is_file():
        return None
    spec = _parse_memory_file(path, scope=scope, with_body=with_body)
    if spec is None:
        return None
    spec.slug = validate_slug(slug)
    return spec


async def count_memories(user_id: str, *, scope: str = SCOPE_PRIVATE) -> int:
    await _reconcile(user_id, scope)
    rows = await _fetch(
        "SELECT COUNT(*) AS n FROM memories WHERE user_id=? AND scope=?", (user_id, scope)
    )
    return int(rows[0]["n"]) if rows else 0


# ---------------- 写 ----------------


def _read_version(path: Path) -> tuple[str, str]:
    """返回 ``(版本令牌, 原文)``。令牌 = 内容 sha256 前 12 位（对齐 CC 的 mWe）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return "", ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], text


async def write_memory(user_id: str, *, slug: str, title: str = "",
                       description: str = "", body: str = "",
                       kind: str = "", tags: list[str] | None = None,
                       scope: str | None = None,
                       if_version: str | None = None,
                       merge: bool = True) -> WriteResult:
    """新建或更新一条记忆（CAS）。

    ``if_version`` 是乐观并发控制：写侧是后台子代理、读侧是主对话，
    用户还可能在设置页手改同一条 —— 没有 CAS 就是后写覆盖前写，
    而且**用户的手改会静默丢失**（§5.1）。

    ``merge=True``（默认）时，``title`` / ``description`` / ``kind`` / ``tags`` 留空
    表示「沿用文件里已有的值」。这是给**模型**用的语义：模型调
    ``memory_write`` 最常见的形态就是只给 slug + content，此时把元数据抹掉
    会毁掉相关性判断。

    ``merge=False`` 时按传入值原样写入。这是给**设置页**用的语义：
    用户点了「清空描述」，就得真的清空，不能被「留空=沿用」悄悄吞掉。
    两个调用方需要相反的语义，所以必须是显式参数，不能靠猜。
    """
    try:
        slug = validate_slug(slug)
        scope = (scope or SCOPE_PRIVATE).strip().lower()
        path = memory_path(user_id, slug, scope=scope, create_dir=True)
    except (ValueError, PathSecurityError) as e:
        return WriteResult(outcome="refused", message=str(e))

    want_kind = (kind or "").strip().lower()
    if want_kind and want_kind not in KINDS:
        return WriteResult(outcome="refused",
                           message=f"未知记忆类型: {kind}（可选 {KINDS}）")

    if len((body or "").encode("utf-8")) > MAX_MEMORY_BYTES:
        return WriteResult(
            outcome="refused",
            message=f"单条记忆超过 {MAX_MEMORY_BYTES // 1024} KB 上限",
        )

    current_version, current_text = _read_version(path)
    exists = path.is_file()
    if if_version:
        wanted = str(if_version).strip()
        if not exists:
            return WriteResult(outcome="missing",
                               message=f"记忆 {slug} 不存在，无法按 if_version 更新")
        if wanted != current_version:
            return WriteResult(
                outcome="conflict",
                current_version=current_version,
                current_content=current_text,
                message="版本不一致：内容已被其他写入方修改，请基于 currentContent 重放",
            )

    want_title = (title or "").strip()
    want_desc = (description or "").strip()
    want_tags = [str(t).strip() for t in (tags or []) if str(t).strip()] if tags is not None else None

    prev = _parse_memory_file(path, scope=scope) if exists else None
    if merge and prev is not None:
        # merge=True：空值表示「沿用」。
        want_title = want_title or prev.title
        want_desc = want_desc or prev.description
        if want_tags is None:
            want_tags = list(prev.tags)
        if not want_kind:
            want_kind = prev.kind
    if want_tags is None:
        want_tags = []

    spec = MemorySpec(
        slug=slug,
        title=want_title or want_desc[:80] or slug,
        description=want_desc,
        kind=want_kind or KIND_USER,
        tags=tuple(want_tags),
        scope=scope,
        body=body,
        path=path,
        created_at=(prev.created_at if prev is not None else now_ms()),
        updated_at=now_ms(),
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(render_memory(spec))
    except OSError as e:
        return WriteResult(outcome="failed", message=f"写入失败: {e}")

    parsed = _parse_memory_file(path, scope=scope, with_body=False) or spec
    parsed.slug = slug
    try:
        await _upsert_row(user_id, parsed)
    except Exception:  # noqa: BLE001 - 索引失败不该让写入失败
        logger.warning("记忆索引刷新失败（下次列举会自愈）: %s", slug, exc_info=True)
    return WriteResult(outcome="ok", spec=parsed)


async def delete_memory(user_id: str, slug: str, *, scope: str = SCOPE_PRIVATE) -> bool:
    """删除一条记忆。

    保留为**内部能力**，不暴露成工具：CC 客户端只承诺
    ``memory_list`` / ``memory_read`` / ``memory_write`` 三件（§5），
    对外多承诺一个 delete 就是超出契约。删除靠 write 覆盖成空 + 设置页 UI。
    """
    try:
        path = memory_path(user_id, slug, scope=scope)
        slug = validate_slug(slug)
    except (ValueError, PathSecurityError):
        return False
    removed = False
    if path.is_file():
        try:
            path.unlink()
            removed = True
        except OSError:
            return False
    try:
        await _execute(
            "DELETE FROM memories WHERE user_id=? AND scope=? AND slug=?",
            (user_id, scope, slug),
        )
    except Exception:  # noqa: BLE001
        logger.warning("删除记忆索引行失败（下次列举会自愈）: %s", slug, exc_info=True)
    return removed


async def touch_hits(user_id: str, slugs: list[str], *, scope: str = SCOPE_PRIVATE) -> None:
    """记录注入命中（用于将来「最常命中优先」的排序）。失败静默。"""
    if not slugs:
        return
    placeholders = ",".join("?" for _ in slugs)
    try:
        await _execute(
            f"UPDATE memories SET hit_count=hit_count+1, last_hit_at=? "
            f"WHERE user_id=? AND scope=? AND slug IN ({placeholders})",
            (now_ms(), user_id, scope, *slugs),
        )
    except Exception:  # noqa: BLE001
        logger.debug("记忆命中统计失败（忽略）", exc_info=True)


# ---------------- 选择与渲染（注入用） ----------------


def _query_tokens(query_hint: str) -> list[str]:
    """把 query_hint 切成用于关键词命中的词。

    中文没有空格，按空白切会得到整段。这里对中文按 2-gram 粗切，
    对 ASCII 按空白/标点切。做不到分词器的效果，但足够让
    「用户提到 PDF 时优先注入 PDF 相关记忆」这类直觉行为生效。
    """
    text = (query_hint or "").strip().lower()
    if not text:
        return []
    tokens: list[str] = []
    for chunk in re.split(r"[\s,，。；;、:：!！?？()（）\[\]【】\"'`]+", text):
        if not chunk:
            continue
        if re.search(r"[一-鿿]", chunk):
            if len(chunk) <= 3:
                tokens.append(chunk)
            else:
                tokens.extend(chunk[i:i + 2] for i in range(len(chunk) - 1))
        elif len(chunk) >= 2:
            tokens.append(chunk)
    # 去重并保序，最多 64 个
    seen: set[str] = set()
    out: list[str] = []
    for t in tokens:
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 64:
            break
    return out


def _hit_score(m: MemorySpec, tokens: list[str]) -> int:
    """关键词命中计数：description 权重最高（它就是给相关性判断写的）。"""
    if not tokens:
        return 0
    hay_desc = (m.description or "").lower()
    hay_title = (m.title or "").lower()
    hay_tags = [t.lower() for t in m.tags]
    hay_slug = m.slug.lower()
    score = 0
    for t in tokens:
        if t in hay_desc:
            score += 3
        if t in hay_title:
            score += 2
        if any(t in tag for tag in hay_tags):
            score += 3
        if t in hay_slug:
            score += 1
    return score


async def select_for_injection(user_id: str, *, query_hint: str = "",
                               char_budget: int = 0,
                               max_entries: int = 0,
                               scope: str = SCOPE_PRIVATE) -> list[MemorySpec]:
    """按「命中 > 类型优先级 > 最近更新」挑条目，超预算的扣留（不注入但记账）。

    排序规则（§3.2）：``user`` 排最前，因为它承载称呼/语气这类每轮都影响
    表达的信息；``project`` 排最后，因为有效期最短、最容易过时。

    预算按 ``len(str)``（code point）算，不按字节 —— 与索引口径一致（§3.6）。
    """
    items, _withheld = await select_with_stats(
        user_id, query_hint=query_hint, char_budget=char_budget,
        max_entries=max_entries, scope=scope,
    )
    return items


async def select_with_stats(user_id: str, *, query_hint: str = "",
                            char_budget: int = 0, max_entries: int = 0,
                            scope: str = SCOPE_PRIVATE) -> tuple[list[MemorySpec], int]:
    """同 ``select_for_injection``，额外返回「这次扣留了几条」。

    扣留数必须能报出去：用户看到「记忆有 40 条但只注入了 10 条」却不告知，
    和「扣留了并告知」是两种产品（§3.4）。
    """
    await _reconcile(user_id, scope)
    rows = await _fetch(
        "SELECT * FROM memories WHERE user_id=? AND scope=? ORDER BY slug ASC",
        (user_id, scope),
    )
    specs = [_row_to_spec(r, scope=scope, team_key="") for r in rows]
    if not specs:
        return [], 0
    budget = char_budget if char_budget > 0 else inject_budget()
    cap = max_entries if max_entries > 0 else inject_max_entries()
    tokens = _query_tokens(query_hint)

    def sort_key(m: MemorySpec) -> tuple:
        return (
            -_hit_score(m, tokens),
            KIND_PRIORITY.get(m.kind, 9),
            -int(m.updated_at or 0),
            m.slug,
        )

    specs.sort(key=sort_key)
    picked: list[MemorySpec] = []
    used = 0
    for m in specs:
        if len(picked) >= cap:
            break
        line_chars = _entry_line_chars(m)
        # 放不下就跳过这条、继续试更短的 —— 一条 800 字的记忆不该让后面
        # 五条 60 字的记忆全部进不来。CC 也是「扣留单条」而不是「到此为止」。
        if used + line_chars > budget:
            continue
        picked.append(m)
        used += line_chars
    withheld = len(specs) - len(picked)
    return picked, withheld


def _entry_line_chars(m: MemorySpec) -> int:
    """一条记忆在注入块里大概占多少字符（含前缀符号与日期）。"""
    return len(f"- {m.label}：{m.description}（{m.kind}，{'0000-00-00'}）\n")


def _short_day(ts_ms: int) -> str:
    try:
        return time.strftime("%Y-%m-%d", time.localtime(ts_ms / 1000))
    except Exception:  # noqa: BLE001
        return ""


def format_memory_block(specs: list[MemorySpec], char_budget: int = 0,
                        withheld: int = 0) -> str:
    """把选中的记忆渲染成注入块。

    三条设计要点（§3.4）：
      1. 前缀 ``[记忆]`` —— 前端 ``INJECTED_KINDS`` 靠前缀分类；
      2. 明确写「不要逐条复述给用户」 —— 否则模型会把记忆当任务清单念一遍；
      3. 带 type 和日期 —— type 让模型知道分量，日期让它判断时效。

    块尾那行「另有 N 条」不能省：扣留了并告知 和 扣留了不告知 是两种产品。
    """
    if not specs:
        return ""
    lines = ["[记忆] 这个用户的偏好与背景（历史积累，供参考，不要逐条复述给用户）"]
    for m in specs:
        day = _short_day(m.updated_at)
        suffix = f"（{m.kind}" + (f"，{day}" if day else "") + "）"
        lines.append(f"- {m.label}：{m.description}{suffix}")
    if withheld > 0:
        lines.append(f"（另有 {withheld} 条记忆因预算未注入，可用 memory_list 查看）")
    return "\n".join(lines)


# ---------------- 入口索引 MEMORY.md ----------------


async def rebuild_index(user_id: str, *, scope: str = SCOPE_PRIVATE) -> Path:
    """重建 ``MEMORY.md`` 入口索引。

    **唯一的索引改写者**是 dream（§4.6），写侧（memory_write）不碰它 ——
    否则每写一条就重排一次索引，prompt 缓存会不停失效。

    cap 单位是 code point（§3.6），超限时压缩成「只保留最近 N 行」而不是
    中途截断某一行 —— 截断会产生半行 markdown，比少几条更难读。
    """
    await _reconcile(user_id, scope)
    rows = await _fetch(
        "SELECT * FROM memories WHERE user_id=? AND scope=? ORDER BY updated_at DESC, slug ASC",
        (user_id, scope),
    )
    max_chars = index_max_chars()
    max_lines = index_max_lines()
    lines = ["# 记忆索引", "", "> 由 dream 维护，请勿手动编辑（新增记忆请写独立的 .md 文件）", ""]
    dropped = 0
    for r in rows:
        m = _row_to_spec(r, scope=scope, team_key="")
        line = f"- [{m.label}]({m.slug}.md) — {m.description}"
        candidate = "\n".join(lines + [line])
        if len(lines) + 1 > max_lines or len(candidate) > max_chars:
            dropped += 1
            continue
        lines.append(line)
    if dropped:
        lines.append("")
        lines.append(f"（另有 {dropped} 条未列入索引，可用 memory_list 查看）")
    target = index_path(user_id, scope=scope)
    text = "\n".join(lines).rstrip("\n") + "\n"
    with open(target, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return target


def clear_cache() -> None:
    """占位：本模块不缓存任何东西（文件即真相）。留给调用方对齐接口。"""
    return None
