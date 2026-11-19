"""app 侧执行后端（方案 §6.3）。

`get_backend()` 是唯一入口。三种实现：

| backend | 类 | 用途 |
| --- | --- | --- |
| （mode=off） | `OffBackend` | 完全不进沙箱 |
| `service` | `ServiceBackend` | 调独立沙箱服务（默认方向） |
| `inproc` | `InprocBackend` | 现有 Docker 实现，**回滚路径** |
"""
from .base import Backend, VALID_BACKENDS, get_backend

__all__ = ["Backend", "VALID_BACKENDS", "get_backend"]