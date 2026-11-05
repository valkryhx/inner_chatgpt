"""Skill store + built-in tools tests (no network, isolated temp dirs)."""
from __future__ import annotations

import asyncio

from app import workspace


def run(coro):
    return asyncio.run(coro)


def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(workspace, "WORKSPACES_DIR", tmp_path / "workspaces")
    monkeypatch.setattr(workspace, "BUILTIN_SKILLS_DIR", tmp_path / "builtin")
    monkeypatch.setattr(workspace, "SHARED_SKILLS_DIR", tmp_path / "shared")
    (tmp_path / "skills").mkdir()
    (tmp_path / "workspaces").mkdir()
    (tmp_path / "builtin").mkdir()
    (tmp_path / "shared").mkdir()


def test_parse_skill_markdown_frontmatter():
    from app.skill_store import parse_skill_markdown

    text = "---\nname: pdf-report\ndescription: 生成报告\n---\n\n# 正文\nhello"
    meta, body = parse_skill_markdown(text, "fallback")
    assert meta["name"] == "pdf-report"
    assert meta["description"] == "生成报告"
    assert "# 正文" in body


def test_parse_skill_markdown_no_frontmatter():
    from app.skill_store import parse_skill_markdown

    meta, body = parse_skill_markdown("# 标题\n内容", "fallback")
    assert meta["name"] == "fallback"
    assert meta["description"] == "标题"
    assert body == "# 标题\n内容"


def test_safe_workspace_path_rejects_escape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.workspace import PathSecurityError, safe_workspace_path

    ok = safe_workspace_path("u1", "a/b.txt")
    assert str(ok).startswith(str((tmp_path / "workspaces" / "u1").resolve()))
    for bad in ("../x", "../../etc/passwd"):
        try:
            safe_workspace_path("u1", bad)
            assert False, bad
        except PathSecurityError:
            pass


def test_user_skill_crud_and_isolation(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import delete_user_skill, get_skill, list_skills, save_user_skill

    save_user_skill("u1", "my-skill", "desc one", "# Body\nstep 1")
    save_user_skill("u2", "other", "desc two", "# Other")

    names_u1 = {s.name for s in list_skills("u1")}
    names_u2 = {s.name for s in list_skills("u2")}
    assert "my-skill" in names_u1 and "my-skill" not in names_u2
    assert "other" in names_u2 and "other" not in names_u1

    spec = get_skill("u1", "my-skill")
    assert spec is not None and spec.source == "user" and "step 1" in spec.body

    assert delete_user_skill("u1", "my-skill") is True
    assert get_skill("u1", "my-skill") is None


def test_user_skill_overrides_builtin(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import list_skills, save_user_skill

    builtin_dir = tmp_path / "builtin" / "shared"
    builtin_dir.mkdir(parents=True)
    (builtin_dir / "SKILL.md").write_text("---\nname: shared\ndescription: builtin\n---\nbuiltin body", encoding="utf-8")

    skills = {s.name: s for s in list_skills("u1")}
    assert skills["shared"].source == "builtin"

    save_user_skill("u1", "shared", "user version", "user body")
    skills = {s.name: s for s in list_skills("u1")}
    assert skills["shared"].source == "user"
    assert "user body" in skills["shared"].body


def test_invalid_skill_name(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import save_user_skill

    for bad in ("../evil", "Bad Name", "a/b", ""):
        try:
            save_user_skill("u1", bad, "", "x")
            assert False, bad
        except ValueError:
            pass


def test_builtin_tools_file_roundtrip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools("u1")}
    assert {"file_read", "file_write", "file_patch", "list_files", "code_run", "load_skill"} <= set(tools)

    out = run(tools["file_write"].call({"path": "dir/a.txt", "content": "hello\nworld\n"}))
    assert "已写入" in out

    read = run(tools["file_read"].call({"path": "dir/a.txt"}))
    assert "1|hello" in read and "2|world" in read

    patched = run(tools["file_patch"].call({"path": "dir/a.txt", "old_content": "world", "new_content": "WORLD"}))
    assert "已局部修改" in patched
    read2 = run(tools["file_read"].call({"path": "dir/a.txt", "show_linenos": False}))
    assert "WORLD" in read2 and "world" not in read2

    # patch must report ambiguity on duplicate matches
    run(tools["file_write"].call({"path": "dup.txt", "content": "x\nx\n"}))
    dup = run(tools["file_patch"].call({"path": "dup.txt", "old_content": "x", "new_content": "y"}))
    assert "工具执行错误" in dup and "2 处" in dup


def test_builtin_tools_reject_escape(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools("u1")}
    bad = run(tools["file_write"].call({"path": "../escape.txt", "content": "x"}))
    assert bad.startswith("[工具执行错误]")


def test_code_run_python(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.builtin_tools import build_builtin_tools

    tools = {t.name: t for t in build_builtin_tools("u1")}
    out = run(tools["code_run"].call({"code": "print(2+3)", "type": "python"}))
    assert "exit=0" in out and "5" in out


def test_load_skill_tool(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.builtin_tools import build_builtin_tools
    from app.skill_store import save_user_skill

    save_user_skill("u1", "demo", "演示", "目录=${SKILL_DIR}\n参数=$ARGUMENTS")
    tools = {t.name: t for t in build_builtin_tools("u1")}
    out = run(tools["load_skill"].call({"skill": "demo", "args": "abc"}))
    assert "Skill: demo" in out and "参数=abc" in out and "${SKILL_DIR}" not in out

    missing = run(tools["load_skill"].call({"skill": "nope"}))
    assert missing.startswith("[工具执行错误]") and "未知 Skill" in missing


def test_skill_prompt_listing(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import build_skill_prompt, save_user_skill

    save_user_skill("u1", "one", "第一个", "body")
    prompt = build_skill_prompt("u1")
    assert "[Available Skills]" in prompt and "- one: 第一个" in prompt


# ---------------- skill bundle (zip) import ----------------

def _zip_bytes(entries):
    """Build an in-memory zip from {name: bytes|str}."""
    import io as _io
    import zipfile

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in entries.items():
            data = content.encode("utf-8") if isinstance(content, str) else content
            zf.writestr(name, data)
    return buf.getvalue()


def test_import_single_skill_at_root(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import get_skill, install_user_skill_bundle

    data = _zip_bytes({
        "SKILL.md": "---\nname: 技术报告\ndescription: 写报告\n---\n# body\n用 ${SKILL_DIR}",
        "REFERENCE.md": "ref",
        "scripts/run.py": "print(1)",
    })
    # Chinese frontmatter name + no wrapper dir -> slug passed by the caller.
    names = install_user_skill_bundle("u1", data, default_name="tech-report")
    assert names == ["tech-report"]
    # frontmatter name was normalised to a loadable slug
    spec = get_skill("u1", "tech-report")
    assert spec is not None
    assert "name: tech-report" in (spec.root / "SKILL.md").read_text(encoding="utf-8")
    assert (spec.root / "REFERENCE.md").is_file()
    assert (spec.root / "scripts" / "run.py").is_file()


def test_import_multi_skill_dirs(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import list_skills, install_user_skill_bundle

    data = _zip_bytes({
        "alpha/SKILL.md": "---\nname: alpha\ndescription: A\n---\nA body",
        "alpha/ref.md": "a-ref",
        "beta/SKILL.md": "---\nname: beta\ndescription: B\n---\nB body",
    })
    names = install_user_skill_bundle("u1", data)
    assert sorted(names) == ["alpha", "beta"]
    found = {s.name: s for s in list_skills("u1")}
    assert "alpha" in found and "beta" in found
    assert (found["alpha"].root / "ref.md").is_file()


def test_import_rejects_traversal(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import BundleError, install_user_skill_bundle

    data = _zip_bytes({"SKILL.md": "---\nname: ok\n---\nx", "../evil.txt": "boom"})
    try:
        install_user_skill_bundle("u1", data)
        assert False, "should reject traversal"
    except BundleError as e:
        assert "非法路径" in str(e) or "越界" in str(e)


def test_import_requires_skill_md(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import BundleError, install_user_skill_bundle

    try:
        install_user_skill_bundle("u1", _zip_bytes({"readme.txt": "no skill here"}))
        assert False
    except BundleError as e:
        assert "SKILL.md" in str(e)


def test_import_rejects_bad_zip(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import BundleError, install_user_skill_bundle

    try:
        install_user_skill_bundle("u1", b"not a zip at all")
        assert False
    except BundleError as e:
        assert "zip" in str(e)


def test_import_same_name_requires_overwrite(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import BundleError, get_skill, install_user_skill_bundle

    data = _zip_bytes({"demo/SKILL.md": "---\nname: demo\n---\nfirst"})
    install_user_skill_bundle("u1", data)
    try:
        install_user_skill_bundle("u1", data)
        assert False, "should collide"
    except BundleError as e:
        assert "已存在同名" in str(e)

    data2 = _zip_bytes({"demo/SKILL.md": "---\nname: demo\n---\nsecond"})
    install_user_skill_bundle("u1", data2, overwrite=True)
    assert "second" in get_skill("u1", "demo").body


def test_shared_skill_visible_and_overridable(tmp_path, monkeypatch):
    _isolate(tmp_path, monkeypatch)
    from app.skill_store import install_shared_skill_bundle, list_skills, save_user_skill

    install_shared_skill_bundle(_zip_bytes({
        "pub/SKILL.md": "---\nname: pub\ndescription: public\n---\npublic body",
    }))
    skills = {s.name: s for s in list_skills("u1")}
    assert skills["pub"].source == "shared"
    assert "public body" in skills["pub"].body

    save_user_skill("u1", "pub", "mine", "private body")
    skills = {s.name: s for s in list_skills("u1")}
    assert skills["pub"].source == "user"
    assert "private body" in skills["pub"].body


def test_shared_skill_readable_by_file_read(tmp_path, monkeypatch):
    """Bundled files in a shared skill must be readable via file_read path safety."""
    _isolate(tmp_path, monkeypatch)
    from app.builtin_tools import build_builtin_tools
    from app.skill_store import install_shared_skill_bundle

    install_shared_skill_bundle(_zip_bytes({
        "pub/SKILL.md": "---\nname: pub\n---\nbody",
        "pub/references/guide.md": "GUIDE-CONTENT",
    }))
    tools = {t.name: t for t in build_builtin_tools("u1")}
    out = run(tools["file_read"].call({"path": str(tmp_path / "shared" / "pub" / "references" / "guide.md")}))
    assert "GUIDE-CONTENT" in out


# ---------------- runtime cache invalidation ----------------

def test_runtime_invalidate_all_drops_every_user():
    """Imported shared skills must invalidate ALL cached runtimes, not one user."""
    from app.runtime import RuntimeHub

    class FakeRuntime:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def scenario():
        hub = RuntimeHub()
        a, b, c = FakeRuntime(), FakeRuntime(), FakeRuntime()
        hub._runtimes[("u1", "")] = a
        hub._runtimes[("u1", "llm_x")] = b
        hub._runtimes[("u2", "")] = c

        dropped = await hub.invalidate_all()
        assert dropped == 3
        assert hub._runtimes == {}
        assert a.closed and b.closed and c.closed

        # 幂等：再调一次不会报错，也没什么可删
        assert await hub.invalidate_all() == 0

    run(scenario())


def test_runtime_invalidate_scoped_to_user():
    from app.runtime import RuntimeHub

    class FakeRuntime:
        def __init__(self):
            self.closed = False

        async def close(self):
            self.closed = True

    async def scenario():
        hub = RuntimeHub()
        a, b = FakeRuntime(), FakeRuntime()
        hub._runtimes[("u1", "")] = a
        hub._runtimes[("u2", "")] = b
        await hub.invalidate("u1")
        assert ("u1", "") not in hub._runtimes
        assert ("u2", "") in hub._runtimes
        assert a.closed and not b.closed

    run(scenario())
