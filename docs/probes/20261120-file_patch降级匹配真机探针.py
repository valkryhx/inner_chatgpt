"""真机探针：沙箱后端下 file_patch 的降级匹配是否生效。

## 为什么要真机

单测走的是进程内 `_patch_impl`，而生产走的是
`builtin_tools.file_patch → sandbox.manager.patch_file → service backend`
这条完全不同的路径。第二层里匹配与替换都在宿主做完、只把结果发给容器，
所以「宿主侧改了、服务侧没改」这类错单测抓不到。

## 判据（能区分两种状态）

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 文件里是智能引号，old 是 ASCII | 找不到 | 成功 |
| 文件里是 `\\u4F60`，old 是「你」 | 找不到 | 成功 |
| replace_all 三处 | 找不到/只改一处 | 三处全改 |

每条都**读回文件内容**验证，不只看返回值。
"""
from __future__ import annotations

import asyncio
import json
import sys

sys.path.insert(0, "/app/backend")
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app import builtin_tools as bt  # noqa: E402
from app.sandbox import policy as sbx_policy  # noqa: E402

UID = "u_c56b8337dd09"


async def find_patch_tool():
    tools = bt.build_builtin_tools(UID, session_id="probe", generation_id="probe")
    for t in tools:
        if t.name == "file_patch":
            return t
    raise SystemExit("找不到 file_patch 工具")


async def main() -> int:
    print(f"should_use_sandbox(file_patch) = {sbx_policy.should_use_sandbox('file_patch', UID)}")

    tools = bt.build_builtin_tools(UID, session_id="probe", generation_id="probe")
    by_name = {t.name: t for t in tools}
    write = by_name["file_write"]
    patch = by_name["file_patch"]

    base = "probe_tier"
    fails = []

    async def check(label, path, content, old, new, expect_in, extra_args=None,
                    expect_absent=None):
        await write.call({"path": path, "content": content})
        args = {"path": path, "old_content": old, "new_content": new}
        args.update(extra_args or {})
        out = await patch.call(args)
        back = await by_name["file_read"].call({"path": path})
        # ⚠️ file_read 返回带行号前缀（`1|bar`），所以断言不能直接用
        # `"bar\nbar\nbar" in back` —— 那永远不成立。去掉前缀再比。
        plain = "\n".join(
            ln.split("|", 1)[1] if "|" in ln else ln
            for ln in str(back).splitlines())
        ok = expect_in in plain and "未找到" not in str(out)
        if expect_absent and expect_absent in plain:
            ok = False
        mark = "✓" if ok else "✗"
        print(f"  [{mark}] {label}")
        print(f"      out={str(out)[:110]}")
        if not ok:
            fails.append(label)
            print(f"      read_back={str(back)[:160]}")
        return ok

    print("\n=== ① 精确匹配（回归）===")
    await check("精确替换", f"{base}_a.txt", "hello world\n",
                "hello world", "goodbye", "goodbye")

    print("\n=== ② 智能引号 ↔ ASCII ===")
    await check("文件智能引号 / old 是 ASCII", f"{base}_b.py",
                'msg = “你好”\nprint(1)\n',
                'msg = "你好"', 'msg = "OK"', '"OK"')
    await check("文件 ASCII / old 是智能引号", f"{base}_c.py",
                'msg = "你好"\n', 'msg = “你好”', 'msg = "OK2"', '"OK2"')

    print("\n=== ③ 文件真中文 / old 是转义字面量 ===")
    await check("\\uXXXX 还原", f"{base}_d.js", "const s = '你好';\n",
                "const s = '\\u4f60\\u597d';", "const s = 'ZZZ';", "ZZZ")

    print("\n=== ④ 文件转义 / old 是真中文 ===")
    await check("字符→转义", f"{base}_e.json", '{"k": "\\u4F60\\u597D"}\n',
                '"你好"', '"X"', '"X"')

    print("\n=== replace_all ===")
    await check("三处全替换", f"{base}_f.txt", "foo\nfoo\nfoo\n",
                "foo", "bar", "bar\nbar\nbar",
                extra_args={"replace_all": True})
    await check("未开 replace_all 多处应拒绝", f"{base}_g.txt", "foo\nfoo\n",
                "foo", "bar", "foo\nfoo")

    print()
    if fails:
        print(f"✗ 失败 {len(fails)} 项：{fails}")
        return 1
    print("✓ 全部通过")
    return 0


sys.exit(asyncio.run(main()))