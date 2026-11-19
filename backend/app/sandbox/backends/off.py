"""`off` 后端：什么都不做。

## 为什么单独一个类

「关掉沙箱」有两种实现方式：

```python
# 方式 A：在调用方加 if
if cfg.mode != "off":
    ...沙箱逻辑...

# 方式 B：off 也是一个后端对象
backend = get_backend()   # mode=off 时返回 OffBackend
backend.exec_code(...)    # 立刻返回 handled=False
```

选 B，因为方案 §6.4 要求 `off` 时**网络层 0 次调用**。有一。

## 它绝不做什么

不构造客户端、不读服务地址、不发 HTTP。`handled=False` 让调用方走进程内路径
（与现在 `HLZ_SANDBOX_MODE=off` 的行为逐字节一致）。
"""
from __future__ import annotations


class OffBackend:
    """`mode=off` 专用：立刻交回调用方，什么都不碰。"""

    name = "off"

    async def health(self) -> tuple[bool, str]:
        """恒健康：`off` 什么都不做，不存在「不可用」这种状态。"""
        return True, "off（未启用沙箱）"

    async def exec_code(
        self,
        user_id: str,
        code: str,
        *,
        code_type: str = "python",
        timeout: float = 60.0,
        root: str = "",
        session_id: str = "",
        generation_id: str = "",
        agent_id: str = "root",
    ) -> tuple[bool, str, str]:
        # handled=False ⇒ 调用方退回进程内。
        # 不返回任何降级提示文本：off 是**正常状态**，不是「降级」。
        return False, "", ""

    async def write_file(self, *a, **kw) -> tuple[bool, str]:
        return False, ""

    async def patch_file(self, *a, **kw) -> tuple[bool, str]:
        return False, ""