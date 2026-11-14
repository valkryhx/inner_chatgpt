"""查一个 deminified 模块里某个符号是从哪个 chunk 导入的（build_chunk_map 的反向操作）。

用途：读源码时看到 `import { x } from "B:/~BUN/root/chunk-yyy.js"` 想知道 yyy 是谁，
或者看到 `x(...)` 想知道它到底来自哪个模块 —— 比 grep 全仓快，也不会被同名混淆。

用法：
    python tools/cc_source_verify/resolve_import.py <模块文件路径> <符号> [符号...]

例：
    python tools/cc_source_verify/resolve_import.py m0591_0x11be624e.js SUo gm
    → SUo -> B:/~BUN/root/chunk-xxxx.js
      gm -> B:/~BUN/root/chunk-xxxx.js

输出 `NOT IMPORTED` 表示该符号是本模块自己定义的（局部函数/变量），不是跨模块引用。
拿到 chunk 名后，用 build_chunk_map.py 查它对应哪个 mNNNN。
"""
import os
import re
import sys

DEFAULT_ROOT = r"D:\git_repos\claude_hack\reconstructed319\deminified"


def parse_imports(text):
    """解析多行 ES import 块 → {本地名: (源, 原始名或 'default')}。"""
    out = {}
    for m in re.finditer(
        r"^import\s*(?:([\w$]+)\s*,)?\s*(?:\{([^}]*)\})?\s*from\s*\"([^\"]+)\";",
        text, re.M | re.S):
        default, named, src = m.group(1), m.group(2), m.group(3)
        if default:
            out[default] = (src, "default")
        if named:
            for piece in named.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if " as " in piece:
                    orig, alias = [p.strip() for p in piece.split(" as ")]
                    out[alias] = (src, orig)
                else:
                    out[piece] = (src, piece)
    return out


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    if len(argv) < 2:
        print(__doc__)
        return 2
    path, syms = argv[0], argv[1:]
    if not os.path.exists(path):
        # 允许只给文件名，从 CC_DEMINIFIED_ROOT 找
        alt = os.path.join(os.environ.get("CC_DEMINIFIED_ROOT") or DEFAULT_ROOT, path)
        if os.path.exists(alt):
            path = alt
        else:
            print(f"文件不存在: {path}")
            return 1

    imp = parse_imports(open(path, encoding="utf-8").read())
    for s in syms:
        if s in imp:
            src, orig = imp[s]
            alias = "" if orig == s else f"  (as {orig})"
            print(f"{s} -> {src}{alias}")
        else:
            print(f"{s} -> NOT IMPORTED (本模块局部定义，或从 default 导出解构)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
