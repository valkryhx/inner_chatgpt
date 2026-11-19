"""归档层的测试。

要点：
1. **稳定 key**（不含实例 id / 时间戳）
2. **越界防护**（key 里带 `..` 要拒绝）
3. **tar 解包防穿越**（成员名带 `../` 要拒绝）
4. `put` 是原子的（先写临时文件再 rename）
"""
from __future__ import annotations

import io
import sys
import tarfile
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def test_archive_key_is_stable(tmp_path):
    """key 只由 user_id + session_id 决定，不含任何实例/时间信息。"""
    from sandbox_service.archive import archive_key

    k1 = archive_key("u1", "sess_a")
    k2 = archive_key("u1", "sess_a")
    assert k1 == k2 == "u1/sess_a.tgz"
    # 换一个 session 才是另一个 key
    assert archive_key("u1", "sess_b") != k1


def test_archive_key_without_session(tmp_path):
    from sandbox_service.archive import archive_key

    assert archive_key("u1") == "u1/_default.tgz"


def test_archive_key_rejects_escape():
    from sandbox_service.archive import archive_key
    from sandbox_service.workspace import PathSecurityError

    with pytest.raises(PathSecurityError):
        archive_key("../evil")
    with pytest.raises(PathSecurityError):
        archive_key("u1", "../../evil")


def test_vol_put_get_roundtrip(tmp_path):
    from sandbox_service.archive import VolBackend

    b = VolBackend(tmp_path)
    b.put("u1/s.tgz", b"hello")
    assert b.get("u1/s.tgz") == b"hello"
    assert b.get("u1/missing.tgz") is None


def test_vol_delete_is_idempotent(tmp_path):
    from sandbox_service.archive import VolBackend

    b = VolBackend(tmp_path)
    b.delete("u1/never.tgz")  # 不抛
    b.put("u1/s.tgz", b"x")
    b.delete("u1/s.tgz")
    assert b.get("u1/s.tgz") is None


def test_vol_rejects_key_escape(tmp_path):
    from sandbox_service.archive import VolBackend

    b = VolBackend(tmp_path / "archives")
    with pytest.raises(ValueError):
        b.put("../../escaped.tgz", b"x")


def test_pack_unpack_roundtrip(tmp_path):
    from sandbox_service.archive import pack_dir, unpack_dir

    src = tmp_path / "src"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A", encoding="utf-8")
    (src / "sub" / "b.txt").write_text("B", encoding="utf-8")

    data = pack_dir(src)
    dst = tmp_path / "dst"
    unpack_dir(data, dst)

    assert (dst / "a.txt").read_text(encoding="utf-8") == "A"
    assert (dst / "sub" / "b.txt").read_text(encoding="utf-8") == "B"


def test_pack_respects_max_bytes(tmp_path):
    """超上限要报错，防止巨大 workspace 吃光内存。

    ⚠️ 测试数据必须是**难压缩**的：第一版用 `b"x" * 100_000`，
    gzip 把它压到几百字节，于是 `max_bytes=1000` 根本没超，测试假红。
    用随机字节（`os.urandom`）才压不动。
    """
    import os

    from sandbox_service.archive import pack_dir

    src = tmp_path / "src"
    src.mkdir()
    (src / "big.bin").write_bytes(os.urandom(200_000))

    with pytest.raises(ValueError, match="上限"):
        pack_dir(src, max_bytes=1000)


def test_unpack_blocks_path_traversal(tmp_path):
    """★ 安全：tar 成员名带 `../` 必须在解包前被拒绝。

    tarfile 的 `filter="data"` 会拦；这条测试保证我们没把它漏掉。
    """
    from sandbox_service.archive import unpack_dir

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name="../../evil.txt")
        payload = b"pwned"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))

    dst = tmp_path / "safe"
    with pytest.raises(Exception):
        unpack_dir(buf.getvalue(), dst)
    # 关键：真的没写到外面
    assert not (tmp_path.parent / "evil.txt").exists()


def test_put_is_atomic(tmp_path):
    """put 中途失败不能留下半个文件（先写 .tmp 再 rename）。"""
    from sandbox_service.archive import VolBackend

    b = VolBackend(tmp_path)
    b.put("u1/s.tgz", b"v1")

    # 模拟写入过程：确认没有残留 .tmp
    files = list(tmp_path.rglob("*"))
    assert not [f for f in files if f.name.endswith(".tmp")], files
    assert b.get("u1/s.tgz") == b"v1"