"""M1：记忆存储层（目录 + DB 索引 + CRUD + 隔离 + 分页 + CAS）。

对应 ``docs/20261112-Hlaudz记忆系统实施方案.md`` §7.1 的 M-1 / M-2 / M-3 / M-11 / M-12。
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app import memory_store as ms

sys.stdout.reconfigure(encoding="utf-8")


def run(coro):
    return asyncio.run(coro)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    """把记忆目录与 DB 都指到 tmp —— 不碰真实 backend/data。"""
    monkeypatch.setattr(ms, "MEMORIES_DIR", tmp_path / "memories")
    monkeypatch.setattr(ms, "DB_PATH", tmp_path / "app.db")
    run(ms.init_tables())
    return tmp_path


def _write(user="u1", slug="greet", **kw):
    kw.setdefault("title", "称呼")
    kw.setdefault("description", "用户希望被叫老徐")
    kw.setdefault("body", "# 称呼\n\n用户希望被叫「老徐」。\n")
    return run(ms.write_memory(user, slug=slug, **kw))


# ---------------- M-1 跨用户路径越界 ----------------
def test_m1_path_isolation_between_users(store):
    a = _write("userA", "shared-name")
    assert a.ok
    b = _write("userB", "shared-name", description="B 自己的")
    assert b.ok

    root_a = ms.memory_root("userA")
    root_b = ms.memory_root("userB")
    # 同名 slug 落在各自目录下，互不覆盖
    assert (root_a / "private" / "shared-name.md").is_file()
    assert (root_b / "private" / "shared-name.md").is_file()
    assert ms.get_memory.__module__  # 抑制未使用告警的占位

    spec_a = run(ms.get_memory("userA", "shared-name"))
    spec_b = run(ms.get_memory("userB", "shared-name"))
    assert spec_a is not None and spec_b is not None
    assert "老徐" in spec_a.description + spec_a.body
    assert "B 自己的" in spec_b.description
    assert spec_a.path != spec_b.path


def test_m1_unsafe_user_segment_rejected(store):
    from app.workspace import PathSecurityError

    for bad in ("..", "../userA", "a/b", "a\\b", ""):
        with pytest.raises(PathSecurityError):
            ms.memory_root(bad)


def test_m1_unsafe_slug_rejected(store):
    for bad in ("../escape", "a/b", "..", "", "UPPER!!"):
        res = run(ms.write_memory("u1", slug=bad, title="x", description="y", body="z"))
        assert res.outcome == "refused", bad
    # 落盘目录里不应出现任何逃逸产物
    assert list((store / "memories").rglob("*.md")) == []


def test_m1_path_layer_is_a_second_line_of_defense(store, monkeypatch):
    """路径校验必须独立成立：即使 slug 校验被绕过，也写不出目录。

    为什么单独立这条：``validate_slug`` 是第一层（字符集 + 拒绝 ``..``），
    它已经挡住了所有已知输入，所以**只测 write_memory 无法证明**
    ``_resolve_under`` 那一层还在（反向确认时删掉它测试仍全绿）。
    这里把第一层短路掉，直接验证第二层。
    """
    monkeypatch.setattr(ms, "validate_slug", lambda s: str(s))
    from app.workspace import PathSecurityError

    with pytest.raises(PathSecurityError):
        ms.memory_path("u1", "../../userB/private/steal")
    with pytest.raises(PathSecurityError):
        ms.memory_path("u1", "..\\escape")


# ---------------- M-2 DB 查询隔离 ----------------
def test_m2_list_is_per_user(store):
    for i in range(3):
        _write("userA", f"a{i}", description=f"A 的第 {i} 条")
        _write("userB", f"b{i}", description=f"B 的第 {i} 条")

    page_a = run(ms.list_memories("userA", limit=50))
    page_b = run(ms.list_memories("userB", limit=50))
    assert [m.slug for m in page_a.entries] == ["a0", "a1", "a2"]
    assert [m.slug for m in page_b.entries] == ["b0", "b1", "b2"]
    assert page_a.total == 3 and page_b.total == 3
    # 交叉读：B 的 slug 在 A 的库里读不到
    assert run(ms.get_memory("userA", "b0")) is None
    assert run(ms.get_memory("userB", "a0")) is None


def test_m2_handwritten_file_becomes_visible(store):
    """用户手动丢一个 .md 进目录（§8 最小可用集），列举必须看得到。"""
    root = ms.scope_dir("u1", create=True)
    (root / "hand-written.md").write_text(
        "---\nname: hand-written\ndescription: 手写的一条\nmetadata:\n  type: feedback\n---\n正文\n",
        encoding="utf-8",
        newline="\n",
    )
    page = run(ms.list_memories("u1"))
    assert [m.slug for m in page.entries] == ["hand-written"]
    assert page.entries[0].kind == "feedback"
    assert page.entries[0].description == "手写的一条"


# ---------------- M-3 同用户同 slug 唯一 ----------------
def test_m3_same_slug_updates_in_place(store):
    first = _write("u1", "prefs", title="偏好", description="v1", body="第一版\n")
    assert first.ok
    path = first.spec.path
    mtime_1 = path.stat().st_mtime_ns

    second = _write("u1", "prefs", title="偏好", description="v2", body="第二版\n")
    assert second.ok
    assert second.spec.path == path
    assert path.stat().st_mtime_ns >= mtime_1

    assert len(list((store / "memories" / "u1" / "private").glob("*.md"))) == 1
    assert run(ms.count_memories("u1")) == 1
    got = run(ms.get_memory("u1", "prefs"))
    assert got is not None and "第二版" in got.body and got.description == "v2"


def test_m3_partial_update_keeps_metadata(store):
    """只给 slug+body 的调用不能把 title/description/kind 抹掉。"""
    _write("u1", "prefs", title="称呼", description="叫老徐", kind="user", body="v1\n")
    res = run(ms.write_memory("u1", slug="prefs", body="v2 只改了正文\n"))
    assert res.ok
    got = run(ms.get_memory("u1", "prefs"))
    assert got.title == "称呼"
    assert got.description == "叫老徐"
    assert got.kind == "user"
    assert "v2 只改了正文" in got.body


# ---------------- M-11 分页 ----------------
def test_m11_cursor_pagination_60_entries(store):
    for i in range(60):
        _write("u1", f"m{i:03d}", description=f"第 {i} 条")

    page1 = run(ms.list_memories("u1"))
    assert len(page1.entries) == 50
    assert page1.remaining == 10
    assert page1.total == 60
    assert page1.next_cursor == page1.entries[-1].slug

    page2 = run(ms.list_memories("u1", cursor=page1.next_cursor))
    assert len(page2.entries) == 10
    assert page2.remaining == 0
    assert page2.next_cursor == ""
    # 两页无重叠、合起来是全集
    slugs = [m.slug for m in page1.entries] + [m.slug for m in page2.entries]
    assert len(slugs) == 60 and len(set(slugs)) == 60


def test_m11_invalid_cursor_does_not_crash(store):
    for i in range(3):
        _write("u1", f"m{i}", description=f"第 {i} 条")
    for bad in ("../nope", "!!!", "不合法", "m999"):
        page = run(ms.list_memories("u1", cursor=bad))
        # 坏游标按从头开始；越界游标得到空页（都不抛异常）
        assert isinstance(page.entries, list)


def test_m11_query_filter_spans_pages(store):
    for i in range(30):
        _write("u1", f"m{i:02d}", description=("关于 PDF 的" if i % 2 else "关于速度的"))
    page = run(ms.list_memories("u1", limit=5, query="PDF"))
    assert page.total == 15
    assert len(page.entries) == 5
    assert all("PDF" in m.description for m in page.entries)


# ---------------- M-12 CAS ----------------
def test_m12_stale_if_version_conflicts_without_writing(store):
    first = _write("u1", "prefs", description="v1", body="第一版\n")
    assert first.ok
    path = first.spec.path

    # 别人先改了一次
    _write("u1", "prefs", description="v2", body="第二版\n")
    before = path.read_text(encoding="utf-8")
    stale = first.spec.version
    assert stale != ""

    res = run(ms.write_memory("u1", slug="prefs", description="v3",
                              body="第三版\n", if_version=stale))
    assert res.outcome == "conflict"
    assert res.current_version != stale
    assert "第二版" in res.current_content
    # 文件未被改动
    assert path.read_text(encoding="utf-8") == before


def test_m12_fresh_if_version_succeeds(store):
    first = _write("u1", "prefs", description="v1", body="第一版\n")
    got = run(ms.get_memory("u1", "prefs"))
    assert got is not None
    res = run(ms.write_memory("u1", slug="prefs", description="v2",
                              body="第二版\n", if_version=got.version))
    assert res.ok
    assert res.spec is not None and res.spec.description == "v2"


def test_m12_if_version_on_missing_slug(store):
    res = run(ms.write_memory("u1", slug="ghost", description="x",
                              body="y", if_version="deadbeef1234"))
    assert res.outcome == "missing"


# ---------------- 其它存储层不变量 ----------------
def test_delete_removes_file_and_index(store):
    _write("u1", "gone")
    assert run(ms.delete_memory("u1", "gone")) is True
    assert run(ms.get_memory("u1", "gone")) is None
    assert run(ms.count_memories("u1")) == 0
    # 幂等：再删一次返回 False
    assert run(ms.delete_memory("u1", "gone")) is False


def test_body_size_limit(store):
    res = run(ms.write_memory("u1", slug="big", description="x",
                              body="a" * (ms.MAX_MEMORY_BYTES + 1)))
    assert res.outcome == "refused"


def test_unknown_kind_refused(store):
    res = _write("u1", "k", kind="nonsense")
    assert res.outcome == "refused"


def test_team_scope_not_implemented(store):
    with pytest.raises(ValueError):
        ms.scope_dir("u1", "team")


def test_render_roundtrip_preserves_fields(store):
    _write("u1", "rt", title="循环", description="往返一致",
           kind="project", tags=["a", "b"], body="正文第一行\n第二行\n")
    got = run(ms.get_memory("u1", "rt"))
    assert got.kind == "project"
    assert got.tags == ("a", "b")
    assert "正文第一行" in got.body and "第二行" in got.body


def test_files_are_utf8_lf(store):
    _write("u1", "enc", description="中文描述", body="中文正文\n")
    raw = (store / "memories" / "u1" / "private" / "enc.md").read_bytes()
    assert raw.count(b"\r") == 0
    raw.decode("utf-8")  # 不抛即 UTF-8


def test_index_rebuild_respects_line_cap(store, monkeypatch):
    monkeypatch.setattr(ms, "index_max_lines", lambda: 12)
    for i in range(30):
        _write("u1", f"m{i:02d}", description=f"第 {i} 条")
    path = run(ms.rebuild_index("u1"))
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) <= 14  # 12 行上限 + 空行 / 欠账行
    assert "另有" in "\n".join(lines)
