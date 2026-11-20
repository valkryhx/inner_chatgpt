"""反向确认：`sub.tool_start` 的 agent 归属修复是否真的被测住。

## 三个用例

1. **基线**：全绿
2. **把接入侧改回旧写法**（`name=... or aid`，不传 agent）→ 静态守卫必须红
3. **把渲染改回不带 agent** → 归属相关的断言必须红

第 2 组是最重要的：这个缺陷在真机上只表现为「日志字段缺一块」，
既不报错也不影响功能，只有静态守卫能挡住回退。
"""
from __future__ import annotations

import subprocess
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TEST = "backend/tests/test_agent_activity_trace.py"
CHAT = "backend/app/routers/chat.py"
MOD = "backend/app/activity_trace.py"


def run() -> tuple[int, str]:
    r = subprocess.run([sys.executable, "-m", "pytest", TEST, "-q"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def with_patch(path: str, old: str, new: str):
    """临时替换文件内容并在结束后恢复。返回 (rc, out)。"""
    orig = open(path, encoding="utf-8", newline="").read()
    patched = orig.replace(old, new, 1)
    if patched == orig:
        return None, f"注入串没匹配上：{old[:60]!r}"
    try:
        open(path, "w", encoding="utf-8", newline="\n").write(patched)
        rc, out = run()
    finally:
        open(path, "w", encoding="utf-8", newline="\n").write(orig)
    return rc, out


def main() -> int:
    rc, out = run()
    if rc != 0:
        print("✗ 基线不通过：")
        print(out[-700:])
        return 1
    print("[✓] 用例 1（基线）：PASS")

    # 用例 2：接入侧退回旧写法（不传 agent，用 name 兜底）
    old_call = '''trace.record(f"sub.{it}", name=str(inner.get("name") or ""),
                             agent=aid,'''
    new_call = '''trace.record(f"sub.{it}", name=str(inner.get("name") or aid),'''
    rc2, out2 = with_patch(CHAT, old_call, new_call)
    if rc2 is None:
        print(f"✗ [接入侧回退] {out2}")
        return 1
    if rc2 == 0:
        print("✗ [接入侧回退] 静态守卫没有变红 ⇒ 守卫无效")
        print(out2[-600:])
        return 1
    if "agent" not in out2:
        print("[!] 变红了但失败信息里没提 agent，检查是否因别的原因失败")
    print("[✓] 用例 2（接入侧回退旧写法）：FAIL（静态守卫生效）")

    # 用例 3：渲染去掉 agent
    old_render = '''    if agent and agent != who:
        who = f"{who}/{agent}"'''
    new_render = '''    pass  # [注入] 不拼 agent'''
    rc3, out3 = with_patch(MOD, old_render, new_render)
    if rc3 is None:
        print(f"✗ [渲染去掉 agent] {out3}")
        return 1
    if rc3 == 0:
        print("✗ [渲染去掉 agent] 测试仍全绿 ⇒ 归属断言无效")
        return 1
    print("[✓] 用例 3（渲染去掉 agent）：FAIL（符合预期）")

    # 恢复后必须重新 PASS
    rc4, _ = run()
    if rc4 != 0:
        print("✗ 恢复后仍失败，文件可能处于半改状态")
        return 1
    print("[✓] 用例 4（恢复后）：重新 PASS")
    print("\n✓ 反向确认通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())