"""SQLite 存储层：连接/建表 + 多用户 DAO。

多用户铁律（对齐 Aivory 所有权层）：会话/消息读写在 SQL 里强制带 user_id 过滤，
查不到即视为不存在（上层返回 404），即使拿到别人的 id 也无法操作。
"""
from __future__ import annotations

import json
import re
import secrets
import time
from typing import Any, Optional

import aiosqlite

from .config import DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    username      TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'user',
    status        TEXT NOT NULL DEFAULT 'active',
    token_ver     INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    jti        TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    expires_at INTEGER NOT NULL,
    revoked    INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,
    user_id       TEXT NOT NULL DEFAULT 'local',
    title         TEXT NOT NULL DEFAULT '新会话',
    model_id      TEXT NOT NULL DEFAULT '',
    provider      TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    updated_at    INTEGER NOT NULL,
    pinned        INTEGER NOT NULL DEFAULT 0,
    archived      INTEGER NOT NULL DEFAULT 0,
    compaction    TEXT NOT NULL DEFAULT '{}',
    reasoning_effort TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
    id            TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    role          TEXT NOT NULL,
    content       TEXT NOT NULL DEFAULT '',
    reasoning     TEXT NOT NULL DEFAULT '',
    blocks        TEXT NOT NULL DEFAULT '[]',
    status        TEXT NOT NULL DEFAULT 'complete',
    created_at    INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_llm_configs (
    id               TEXT PRIMARY KEY,
    user_id          TEXT NOT NULL,
    label            TEXT NOT NULL DEFAULT '',
    wire_api         TEXT NOT NULL DEFAULT 'openai_chat',
    base_url         TEXT NOT NULL DEFAULT '',
    api_key          TEXT NOT NULL DEFAULT '',
    model            TEXT NOT NULL DEFAULT '',
    context_window   INTEGER NOT NULL DEFAULT 128000,
    max_tokens       INTEGER NOT NULL DEFAULT 8192,
    auto_compact_tokens INTEGER,
    hard_limit_tokens   INTEGER,
    reasoning_effort TEXT NOT NULL DEFAULT '',
    is_default       INTEGER NOT NULL DEFAULT 0,
    created_at       INTEGER NOT NULL,
    updated_at       INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS user_mcp_servers (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    transport  TEXT NOT NULL DEFAULT 'http',
    command    TEXT NOT NULL DEFAULT '',
    args       TEXT NOT NULL DEFAULT '[]',
    url        TEXT NOT NULL DEFAULT '',
    headers    TEXT NOT NULL DEFAULT '{}',
    env        TEXT NOT NULL DEFAULT '{}',
    enabled    INTEGER NOT NULL DEFAULT 1,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_users_email ON users(email);
CREATE INDEX IF NOT EXISTS idx_rt_user ON refresh_tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_user ON user_llm_configs(user_id);
CREATE INDEX IF NOT EXISTS idx_mcp_user ON user_mcp_servers(user_id);
"""


def now_ms() -> int:
    return int(time.time() * 1000)


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


async def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("PRAGMA journal_mode=WAL;")
        await conn.executescript(_SCHEMA)
        # 旧库迁移：user_llm_configs 增加压缩阈值列（幂等）。
        for col, decl in (("auto_compact_tokens", "INTEGER"), ("hard_limit_tokens", "INTEGER")):
            try:
                await conn.execute(f"ALTER TABLE user_llm_configs ADD COLUMN {col} {decl}")
            except Exception:
                pass
        for col, decl in (("args", "TEXT NOT NULL DEFAULT '[]'"), ("env", "TEXT NOT NULL DEFAULT '{}'")):
            try:
                await conn.execute(f"ALTER TABLE user_mcp_servers ADD COLUMN {col} {decl}")
            except Exception:
                pass
        # 旧库迁移：sessions 增加会话级思考等级覆盖（空=沿用模型配置）。
        try:
            await conn.execute("ALTER TABLE sessions ADD COLUMN reasoning_effort TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        # 旧库迁移：users 增加唯一登录名 username（邮箱之外的登录标识）。
        try:
            await conn.execute("ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass
        await conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
            "ON users(username) WHERE username<>''"
        )
        # 历史用户补 username：优先用昵称（仅当其唯一可用），否则用邮箱 @ 前缀；
        # 冲突则追加数字后缀，保证唯一。
        cur = await conn.execute("SELECT id,email,name FROM users WHERE username=''")
        legacy = await cur.fetchall()
        for uid, email, name in legacy:
            prefix = (email or "").split("@")[0].strip().lower() or uid
            nickname = (name or "").strip().lower()
            # 昵称要能当登录名（只允许字母数字 _ . -），且未被占用时才优先使用
            base = prefix
            if nickname and re.fullmatch(r"[a-z0-9_.-]{2,32}", nickname):
                # 昵称唯一（同名账号只有它自己）且没被别人的 username 占用，才用它做登录名。
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM users WHERE lower(name)=?", (nickname,)
                )
                same_name = (await cur.fetchone())[0]
                cur = await conn.execute(
                    "SELECT COUNT(*) FROM users WHERE lower(username)=? AND id<>?", (nickname, uid)
                )
                taken_by_other = (await cur.fetchone())[0]
                if same_name == 1 and taken_by_other == 0:
                    base = nickname
            cand, n = base, 1
            while True:
                cur = await conn.execute(
                    "SELECT 1 FROM users WHERE lower(username)=? AND id<>?", (cand, uid)
                )
                if await cur.fetchone() is None:
                    break
                n += 1
                cand = f"{base}{n}"
            await conn.execute("UPDATE users SET username=? WHERE id=?", (cand, uid))
        # 启动自愈：服务重启后，上一进程遗留的 assistant 消息会永远停在 streaming，
        # 前端会一直显示“正在思考”。进程刚启动时不可能有真实生成在跑，全部收敛为 stopped。
        await conn.execute(
            "UPDATE messages SET status='stopped' WHERE role='assistant' AND status='streaming'"
        )
        await conn.commit()


def _row_to_session(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["pinned"] = bool(d.get("pinned"))
    d["archived"] = bool(d.get("archived"))
    try:
        d["compaction"] = json.loads(d.get("compaction") or "{}")
    except Exception:
        d["compaction"] = {}
    return d


def _normalize_legacy_blocks(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """兼容旧数据：早期版本把块按「text → chart → tool」分组落库，丢失了真实顺序。

    旧格式特征：唯一 text 块在最前、其余只有 chart/tool。
    此时把 text 移到最后（文本是最终答复，工具与图表发生在它之前）。
    新格式（按事件顺序累积）不满足该特征，原样返回。
    """
    if not blocks:
        return blocks
    types = [b.get("type") for b in blocks]
    # 新格式含 reasoning/tool 顺序块：绝不重排（否则会把思考挪到错误位置）。
    if "reasoning" in types:
        return blocks
    if types[0] != "text" or "tool" not in types:
        return blocks
    if any(t == "text" for t in types[1:]):
        return blocks
    return [b for b in blocks if b.get("type") != "text"] + [blocks[0]]


def _row_to_message(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    try:
        d["blocks"] = _normalize_legacy_blocks(json.loads(d.get("blocks") or "[]"))
    except Exception:
        d["blocks"] = []
    return d


async def create_session(user_id: str, title: str = "新会话", model_id: str = "", provider: str = "") -> dict[str, Any]:
    sid = new_id("co")
    ts = now_ms()
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO sessions(id,user_id,title,model_id,provider,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (sid, user_id, title or "新会话", model_id, provider, ts, ts),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (sid, user_id))
        row = await cur.fetchone()
    return _row_to_session(row)


async def list_sessions(user_id: str, limit: int = 50, offset: int = 0) -> tuple[list[dict[str, Any]], bool]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM sessions WHERE user_id=? AND archived=0 ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (user_id, limit + 1, offset),
        )
        rows = await cur.fetchall()
    has_more = len(rows) > limit
    return [_row_to_session(r) for r in rows[:limit]], has_more


async def get_session(session_id: str, user_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
        row = await cur.fetchone()
    return _row_to_session(row) if row else None


async def update_session(session_id: str, user_id: str, **fields: Any) -> Optional[dict[str, Any]]:
    allowed = {"title", "pinned", "archived", "model_id", "provider", "compaction",
               "reasoning_effort"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k in ("pinned", "archived"):
            v = 1 if v else 0
        if k == "compaction" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return await get_session(session_id, user_id)
    sets.append("updated_at=?")
    vals.append(now_ms())
    vals.extend([session_id, user_id])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        await conn.commit()
        cur = await conn.execute("SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
        row = await cur.fetchone()
    return _row_to_session(row) if row else None


async def delete_session(session_id: str, user_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM sessions WHERE id=? AND user_id=?", (session_id, user_id))
        deleted = cur.rowcount > 0
        if deleted:
            await conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        await conn.commit()
    return deleted


async def add_message(session_id: str, role: str, content: str = "", reasoning: str = "",
                      blocks: Optional[list] = None, status: str = "complete",
                      message_id: Optional[str] = None) -> dict[str, Any]:
    mid = message_id or new_id("msg")
    ts = now_ms()
    blocks_json = json.dumps(blocks or [], ensure_ascii=False)
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO messages(id,session_id,role,content,reasoning,blocks,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (mid, session_id, role, content, reasoning, blocks_json, status, ts),
        )
        await conn.execute("UPDATE sessions SET updated_at=? WHERE id=?", (ts, session_id))
        await conn.commit()
        cur = await conn.execute("SELECT * FROM messages WHERE id=?", (mid,))
        row = await cur.fetchone()
    return _row_to_message(row)


async def update_message(message_id: str, **fields: Any) -> None:
    allowed = {"content", "reasoning", "blocks", "status"}
    sets, vals = [], []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "blocks" and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return
    vals.append(message_id)
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(f"UPDATE messages SET {', '.join(sets)} WHERE id=?", vals)
        await conn.commit()


async def list_messages(session_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC, rowid ASC", (session_id,)
        )
        rows = await cur.fetchall()
    return [_row_to_message(r) for r in rows]


async def mark_stale_streaming_stopped(session_id: str) -> int:
    """把某会话内遗留的 assistant streaming 消息收敛为 stopped（调用方需自行确认无活跃生成）。"""
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "UPDATE messages SET status='stopped' WHERE session_id=? AND role='assistant' AND status='streaming'",
            (session_id,),
        )
        await conn.commit()
        return cur.rowcount


async def count_messages(session_id: str) -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (session_id,))
        row = await cur.fetchone()
    return int(row[0]) if row else 0


# ---------------- users ----------------
def _row_to_user(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d.pop("password_hash", None)
    return d


async def create_user(email: str, password_hash: str, name: str = "", role: str = "user",
                      username: str = "") -> dict[str, Any]:
    uid = new_id("u")
    ts = now_ms()
    email = email.lower().strip()
    uname = (username or "").strip().lower() or email.split("@")[0]
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO users(id,email,username,password_hash,name,role,status,token_ver,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (uid, email, uname, password_hash, name or uname, role, "active", 0, ts),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM users WHERE id=?", (uid,))
        row = await cur.fetchone()
    return _row_to_user(row)


async def get_user(user_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users WHERE id=?", (user_id,))
        row = await cur.fetchone()
    return _row_to_user(row) if row else None


async def get_user_with_hash_by_email(email: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM users WHERE email=?", (email.lower().strip(),))
        row = await cur.fetchone()
    return dict(row) if row else None


async def get_user_with_hash_by_identifier(identifier: str) -> Optional[dict[str, Any]]:
    """按「邮箱或用户名」查用户（登录用）。

    含 @ 视为邮箱；否则先按 username 精确匹配（不区分大小写），
    再兜底按邮箱前缀匹配（兼容历史用户/用户记不清用户名的情况）。
    """
    ident = (identifier or "").strip().lower()
    if not ident:
        return None
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if "@" in ident:
            cur = await conn.execute("SELECT * FROM users WHERE email=?", (ident,))
        else:
            cur = await conn.execute(
                "SELECT * FROM users WHERE lower(username)=? LIMIT 1", (ident,)
            )
        row = await cur.fetchone()
        if row is None and "@" not in ident:
            # 兜底：邮箱前缀（仅当唯一匹配时生效）
            cur = await conn.execute(
                "SELECT * FROM users WHERE lower(email) LIKE ? LIMIT 2", (ident + "@%",)
            )
            rows = await cur.fetchall()
            if len(rows) == 1:
                row = rows[0]
    return dict(row) if row else None


async def username_exists(username: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM users WHERE lower(username)=? LIMIT 1", (username.strip().lower(),)
        )
        return await cur.fetchone() is not None


async def email_exists(email: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute(
            "SELECT 1 FROM users WHERE email=? LIMIT 1", (email.strip().lower(),)
        )
        return await cur.fetchone() is not None


async def count_users() -> int:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("SELECT COUNT(*) FROM users")
        row = await cur.fetchone()
    return int(row[0]) if row else 0


# ---------------- refresh tokens ----------------
async def add_refresh_token(jti: str, user_id: str, expires_at: int) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute(
            "INSERT INTO refresh_tokens(jti,user_id,expires_at,revoked,created_at) VALUES(?,?,?,0,?)",
            (jti, user_id, expires_at, now_ms()),
        )
        await conn.commit()


async def get_refresh_token(jti: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM refresh_tokens WHERE jti=?", (jti,))
        row = await cur.fetchone()
    return dict(row) if row else None


async def revoke_refresh_token(jti: str) -> None:
    async with aiosqlite.connect(DB_PATH) as conn:
        await conn.execute("UPDATE refresh_tokens SET revoked=1 WHERE jti=?", (jti,))
        await conn.commit()


# ---------------- user llm configs ----------------
def _opt_int(v: Any) -> Optional[int]:
    """空字符串 / None / 0 -> None（表示按窗口比例派生阈值）。"""
    if v is None or v == "":
        return None
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _row_to_llm(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["is_default"] = bool(d.get("is_default"))
    return d


async def list_llm_configs(user_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_llm_configs WHERE user_id=? ORDER BY is_default DESC, created_at ASC", (user_id,)
        )
        rows = await cur.fetchall()
    return [_row_to_llm(r) for r in rows]


async def get_llm_config(user_id: str, config_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_llm_configs WHERE id=? AND user_id=?", (config_id, user_id)
        )
        row = await cur.fetchone()
    return _row_to_llm(row) if row else None


async def get_default_llm_config(user_id: str) -> Optional[dict[str, Any]]:
    """取用户默认模型。

    优先级：
      1. 用户显式勾选默认的那条（is_default=1）——这是唯一"用户说了算"的来源；
      2. 若从未勾选默认（历史数据 / 导入的配置），回退到**最早创建**的可用配置，
         也就是 UI 下拉里的第一项，行为可预期（不会因为编辑别的模型而漂移）；
         并顺手把它标记为默认，让 UI 明确显示"哪条在生效"（一次性自愈）。

    "可用" = 有 api_key + model + base_url。
    """
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_llm_configs WHERE user_id=? AND is_default=1 LIMIT 1", (user_id,)
        )
        row = await cur.fetchone()
        if row is None:
            cur = await conn.execute(
                "SELECT * FROM user_llm_configs WHERE user_id=? "
                "AND api_key<>'' AND model<>'' AND base_url<>'' "
                "ORDER BY created_at ASC, id ASC LIMIT 1", (user_id,)
            )
            row = await cur.fetchone()
            if row is not None:
                # 自愈：把回退选中的这条标记为默认，后续选择明确、不再依赖隐式规则。
                try:
                    await conn.execute(
                        "UPDATE user_llm_configs SET is_default=0 WHERE user_id=?", (user_id,)
                    )
                    await conn.execute(
                        "UPDATE user_llm_configs SET is_default=1 WHERE id=? AND user_id=?",
                        (row["id"], user_id),
                    )
                    await conn.commit()
                    row = dict(row)
                    row["is_default"] = 1
                    return _row_to_llm(row)
                except Exception:
                    pass
    return _row_to_llm(row) if row else None


async def create_llm_config(user_id: str, **f: Any) -> dict[str, Any]:
    cid = new_id("llm")
    ts = now_ms()
    is_default = 1 if f.get("is_default") else 0
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if is_default:
            await conn.execute("UPDATE user_llm_configs SET is_default=0 WHERE user_id=?", (user_id,))
        await conn.execute(
            "INSERT INTO user_llm_configs(id,user_id,label,wire_api,base_url,api_key,model,context_window,max_tokens,auto_compact_tokens,hard_limit_tokens,reasoning_effort,is_default,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, user_id, f.get("label", ""), f.get("wire_api", "openai_chat"), f.get("base_url", ""),
             f.get("api_key", ""), f.get("model", ""), int(f.get("context_window", 128000)),
             int(f.get("max_tokens", 8192)), _opt_int(f.get("auto_compact_tokens")),
             _opt_int(f.get("hard_limit_tokens")), f.get("reasoning_effort", ""), is_default, ts, ts),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM user_llm_configs WHERE id=?", (cid,))
        row = await cur.fetchone()
    return _row_to_llm(row)


async def update_llm_config(user_id: str, config_id: str, **f: Any) -> Optional[dict[str, Any]]:
    allowed = {"label", "wire_api", "base_url", "api_key", "model", "context_window", "max_tokens", "auto_compact_tokens", "hard_limit_tokens", "reasoning_effort", "is_default"}
    sets, vals = [], []
    for k, v in f.items():
        if k not in allowed:
            continue
        if k == "is_default":
            v = 1 if v else 0
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return await get_llm_config(user_id, config_id)
    sets.append("updated_at=?")
    vals.append(now_ms())
    vals.extend([config_id, user_id])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        if f.get("is_default"):
            await conn.execute("UPDATE user_llm_configs SET is_default=0 WHERE user_id=?", (user_id,))
        await conn.execute(f"UPDATE user_llm_configs SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        await conn.commit()
        cur = await conn.execute("SELECT * FROM user_llm_configs WHERE id=? AND user_id=?", (config_id, user_id))
        row = await cur.fetchone()
    return _row_to_llm(row) if row else None


async def delete_llm_config(user_id: str, config_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM user_llm_configs WHERE id=? AND user_id=?", (config_id, user_id))
        await conn.commit()
    return cur.rowcount > 0


# ---------------- user mcp servers ----------------
def _row_to_mcp(row: aiosqlite.Row) -> dict[str, Any]:
    d = dict(row)
    d["enabled"] = bool(d.get("enabled"))
    for k, default in (("headers", {}), ("env", {}), ("args", [])):
        try:
            d[k] = json.loads(d.get(k) or json.dumps(default))
        except Exception:
            d[k] = default
    return d


async def list_mcp_servers(user_id: str) -> list[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_mcp_servers WHERE user_id=? ORDER BY created_at ASC", (user_id,)
        )
        rows = await cur.fetchall()
    return [_row_to_mcp(r) for r in rows]


async def get_mcp_server(user_id: str, server_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute(
            "SELECT * FROM user_mcp_servers WHERE id=? AND user_id=?", (server_id, user_id)
        )
        row = await cur.fetchone()
    return _row_to_mcp(row) if row else None


async def create_mcp_server(user_id: str, **f: Any) -> dict[str, Any]:
    sid = new_id("mcp")
    ts = now_ms()

    def _j(v, default):
        if v is None:
            v = default
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)

    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(
            "INSERT INTO user_mcp_servers(id,user_id,name,transport,command,args,url,headers,env,enabled,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, user_id, f.get("name", ""), f.get("transport", "http"), f.get("command", ""),
             _j(f.get("args"), []), f.get("url", ""), _j(f.get("headers"), {}),
             _j(f.get("env"), {}), 1 if f.get("enabled", True) else 0, ts, ts),
        )
        await conn.commit()
        cur = await conn.execute("SELECT * FROM user_mcp_servers WHERE id=?", (sid,))
        row = await cur.fetchone()
    return _row_to_mcp(row)


async def update_mcp_server(user_id: str, server_id: str, **f: Any) -> Optional[dict[str, Any]]:
    allowed = {"name", "transport", "command", "args", "url", "headers", "env", "enabled"}
    sets, vals = [], []
    for k, v in f.items():
        if k not in allowed:
            continue
        if k == "enabled":
            v = 1 if v else 0
        if k in ("headers", "env", "args") and not isinstance(v, str):
            v = json.dumps(v, ensure_ascii=False)
        sets.append(f"{k}=?")
        vals.append(v)
    if not sets:
        return await get_mcp_server(user_id, server_id)
    sets.append("updated_at=?")
    vals.append(now_ms())
    vals.extend([server_id, user_id])
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        await conn.execute(f"UPDATE user_mcp_servers SET {', '.join(sets)} WHERE id=? AND user_id=?", vals)
        await conn.commit()
        cur = await conn.execute("SELECT * FROM user_mcp_servers WHERE id=? AND user_id=?", (server_id, user_id))
        row = await cur.fetchone()
    return _row_to_mcp(row) if row else None


async def delete_mcp_server(user_id: str, server_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as conn:
        cur = await conn.execute("DELETE FROM user_mcp_servers WHERE id=? AND user_id=?", (server_id, user_id))
        await conn.commit()
    return cur.rowcount > 0


async def get_message(message_id: str) -> Optional[dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cur = await conn.execute("SELECT * FROM messages WHERE id=?", (message_id,))
        row = await cur.fetchone()
    return _row_to_message(row) if row else None
