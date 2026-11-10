"""写前必读门禁：记录「哪个文件被完整读过」，覆盖前校验内容没被别人改过。

对齐 CC / GA 的判定：
- 从未读过 → 拒绝；
- 只读了片段 → 拒绝；
- 读过且 mtime/size 未变 → 放行；
- mtime 变了但内容 sha256 相同 → 放行（并刷新记录）；
- mtime 变了且内容不同 → 拒绝，要求重读。
"""
from __future__ import annotations

import hashlib
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ReadRecord:
    path: str
    mtime_ns: int
    size: int
    complete: bool
    digest: str = ""


def _sha256(p: Path) -> str:
    try:
        h = hashlib.sha256()
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


class ReadRegistry:
    """一次 generation 一份；不跨会话复用，避免「上次读过」被当成「这次读过」。"""

    def __init__(self, max_entries: int = 500) -> None:
        self.max_entries = max_entries
        self._records: "OrderedDict[str, ReadRecord]" = OrderedDict()

    def record(self, path: Path | str, *, complete: bool) -> None:
        p = Path(path)
        try:
            st = p.stat()
        except OSError:
            return
        key = str(p.resolve())
        rec = ReadRecord(key, st.st_mtime_ns, st.st_size, complete, _sha256(p))
        self._records.pop(key, None)
        self._records[key] = rec
        while len(self._records) > self.max_entries:
            self._records.popitem(last=False)

    def _get(self, p: Path) -> Optional[ReadRecord]:
        return self._records.get(str(p.resolve()))

    def check(self, path: Path | str) -> tuple[bool, str]:
        p = Path(path)
        if not p.exists():
            return True, ""  # 新文件不需要先读
        rec = self._get(p)
        if rec is None:
            return False, f"{p.name} 本次会话还没有读过。请先 file_read {p.name}，让写入基于当前内容。"
        if not rec.complete:
            return False, f"{p.name} 只读了片段。请 file_read 读完整文件后再写。"
        try:
            st = p.stat()
        except OSError:
            return False, f"{p.name} 状态不可读，请重新 file_read。"
        if st.st_mtime_ns == rec.mtime_ns and st.st_size == rec.size:
            return True, ""
        if _sha256(p) == rec.digest:
            # 只是 touch，内容没变：刷新记录后放行
            self.record(p, complete=True)
            return True, ""
        return False, f"{p.name} 已被外部修改。请重新 file_read 后再写。"
