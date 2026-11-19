"""归档后端：workspace 的持久化（方案 §7.4）。

## 为什么现在只做卷后端

方案 §7 的判据：三个需求里只有「跨机器共享」需要对象存储。
当前部署是单机 + 共享卷（实测三实例同一 inode），所以 `VolBackend` 够用。

**但接口留出来了** —— 将来上 MinIO 只是新增一个实现类，不动调用方。

## 归档 key 必须是稳定标识

照 Aivory 踩过的坑（它的 `app.py:224` 注释写明了修法）：

> Workspace tarballs are keyed by THIS (conversation id), not the ephemeral
> session id, so a workspace survives session recycle: every create mints a
> fresh session uuid, but the reaper archives — and the next create restores —
> under the same stable key.

用实例 id 做 key 的话，「回收后再唤起」永远恢复不到上次的工作区。
"""
from __future__ import annotations

import io
import tarfile
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class ArchiveBackend(Protocol):
    """归档后端：put / get / delete。"""

    name: str

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...
    def delete(self, key: str) -> None: ...


def archive_key(user_id: str, session_id: str = "") -> str:
    """构造稳定归档 key。

    - 有 session_id：`<user_id>/<session_id>.tgz`（会话级恢复）
    - 没有：`<user_id>/_default.tgz`（用户级）

    **绝不包含实例 id / 时间戳** —— 那样回收后就找不回来了。
    """
    from ..workspace import reject_unsafe_segment

    reject_unsafe_segment(user_id)
    if session_id:
        # session_id 也要过一遍校验（它可能来自用户输入）
        reject_unsafe_segment(session_id)
        return f"{user_id}/{session_id}.tgz"
    return f"{user_id}/_default.tgz"


class VolBackend:
    """卷后端：把 workspace 打成 tar 存到本地目录。

    目录由 `HLZ_SANDBOX_ARCHIVE_DIR` 指定，默认
    `/app/backend/data/sandbox-archives`（在 appdata 卷里）。
    """

    name = "vol"

    def __init__(self, root: Path | None = None) -> None:
        import os

        self._root = Path(
            root
            or os.environ.get("HLZ_SANDBOX_ARCHIVE_DIR")
            or "/app/backend/data/sandbox-archives"
        )

    def _path(self, key: str) -> Path:
        """把 key 映射成路径，并防越界。

        key 里可能有 `/`（`<user_id>/<session>.tgz`），所以不能直接
        `root / key` 就完事——要确认解析后仍在 root 下。
        """
        p = (self._root / key).resolve()
        root_r = self._root.resolve()
        if p != root_r and root_r not in p.parents:
            raise ValueError(f"归档 key 越界: {key!r}")
        return p

    def put(self, key: str, data: bytes) -> None:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        # 先写临时文件再 rename：避免部分写入被当成完整归档
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except FileNotFoundError:
            return None

    def delete(self, key: str) -> None:
        """幂等：删不存在的 key 不报错。"""
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            pass


def pack_dir(src: Path, *, max_bytes: int | None = None) -> bytes:
    """把目录打成 tar.gz 字节流。

    `max_bytes` 给上限时超了抛 `ValueError`——防止一个巨大的 workspace
    把服务内存吃光（Aivory 也有类似护栏，见它的 `MAX_ARCHIVE_BYTES`）。
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for item in sorted(src.rglob("*")):
            if item.is_file():
                tar.add(item, arcname=str(item.relative_to(src)))
    data = buf.getvalue()
    if max_bytes is not None and len(data) > max_bytes:
        raise ValueError(f"归档超过上限：{len(data)} > {max_bytes}")
    return data


def unpack_dir(data: bytes, dst: Path) -> None:
    """把 tar.gz 解到目录。

    ⚠️ 必须防路径穿越：tar 里可以有 `../../etc/passwd` 这种成员名。
    Python 3.12+ 的 `tarfile.data_filter` 能挡，这里显式用它。
    """
    dst.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        # data_filter 会拒绝绝对路径、.. 穿越、设备文件等
        tar.extractall(path=dst, filter="data")