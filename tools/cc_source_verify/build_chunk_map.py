"""把 CC 打包产物里的 chunk specifier 还原成 deminified 模块名。

背景：CC 2.1.319 是 Bun 单文件打包（整个 50.9 MiB JS 都在 claude.exe 的 `.bun` 段里）。
反编译出来的 2,882 个 `mXXXX_0xYYYYYYYY.js` 之间用 `import ... from "B:/~BUN/root/chunk-xxx.js"`
互相引用——但 `chunk-xxx.js` **从来不是磁盘上的文件**，它只是原始模块图里的 specifier。
所以「文档引用了某个 chunk」这件事，必须先还原成 `mNNNN` 才能回源码看。

原理：deminify 改了文件名和本地标识符，但**没有改跨模块导出的名字**。
于是对某个 chunk C，所有模块从 C 导入的具名符号的并集，就等于真实模块 C 的导出列表。
按「交集 / 导入数」打分，取分最高且 ≥0.6 的模块，并检查有无并列（ambiguous）。

用法：
    python tools/cc_source_verify/build_chunk_map.py
    python tools/cc_source_verify/build_chunk_map.py chunk-m1zmpfya   # 顺便查几个
    CC_DEMINIFIED_ROOT=/path/to/deminified python tools/.../build_chunk_map.py

期望输出（本机实测）：
    chunks with named imports: 1764
    mapped: 1764
    ambiguous: 0

见 docs/20261114-CC319记忆机制源码复核v3.md §0.2。
"""
import json
import os
import re
import sys
from collections import defaultdict

CHUNK_PREFIX = "B:/~BUN/root/"
DEFAULT_ROOT = r"D:\git_repos\claude_hack\reconstructed319\deminified"
# 输出到脚本所在目录，换机器也能跑
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "chunk_map.json")


def find_root():
    return os.environ.get("CC_DEMINIFIED_ROOT") or DEFAULT_ROOT


def parse_exports(text):
    """收集一个模块的导出符号：`export {a, b as c}` / `export function` / `export const`。"""
    syms = set()
    for m in re.finditer(r"^export\s*\{([^}]*)\}", text, re.M | re.S):
        body = m.group(1).strip()
        if not body:
            continue
        for piece in body.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if " as " in piece:
                _, alias = [p.strip() for p in piece.split(" as ")]
                syms.add(alias)
            else:
                syms.add(piece)
    for m in re.finditer(r"^export\s+(?:async\s+)?function\s+([\w$]+)", text, re.M):
        syms.add(m.group(1))
    for m in re.finditer(r"^export\s+(?:const|let|var|class)\s+([\w$]+)", text, re.M):
        syms.add(m.group(1))
    return syms


def parse_imports(text):
    """返回 {chunk: set((kind, 原始符号名))}，只保留 B:/~BUN/root/ 的引用。"""
    res = defaultdict(set)
    for m in re.finditer(
        r"^import\s*(?:([\w$]+)\s*,)?\s*(?:\{([^}]*)\})?\s*from\s*\"([^\"]+)\";",
        text, re.M | re.S):
        default, named, src = m.group(1), m.group(2), m.group(3)
        if not src.startswith(CHUNK_PREFIX):
            continue
        chunk = src[len(CHUNK_PREFIX):]
        if default:
            res[chunk].add(("default", default))
        if named:
            for piece in named.split(","):
                piece = piece.strip()
                if not piece:
                    continue
                if " as " in piece:
                    orig, _alias = [p.strip() for p in piece.split(" as ")]
                    res[chunk].add(("named", orig))
                else:
                    res[chunk].add(("named", piece))
    return res


def build(root, out_path, min_score=0.6):
    files = sorted(f for f in os.listdir(root) if f.startswith("m") and f.endswith(".js"))
    if not files:
        raise SystemExit(f"没有在 {root} 找到 m*.js —— 用 CC_DEMINIFIED_ROOT 指定正确路径")

    exports = {}
    imports_by_chunk = defaultdict(set)
    for fn in files:
        text = open(os.path.join(root, fn), encoding="utf-8").read()
        exports[fn] = parse_exports(text)
        for chunk, syms in parse_imports(text).items():
            imports_by_chunk[chunk] |= syms

    by_symbol = defaultdict(set)
    for fn, syms in exports.items():
        for s in syms:
            by_symbol[s].add(fn)

    mapping = {}
    ambiguous = {}
    for chunk, wanted in imports_by_chunk.items():
        named = {v for k, v in wanted if k == "named"}
        if not named:
            # 只有 default import 的 chunk 无法用符号集区分，交给 resolve_import.py 人工看
            continue
        candidates = set()
        for s in named:
            candidates |= by_symbol.get(s, set())
        scored = []
        for fn in candidates:
            inter = len(named & exports[fn])
            scored.append((inter / len(named), inter, fn))
        scored.sort(reverse=True)
        if not scored:
            continue
        top = scored[0]
        if top[0] < min_score:
            continue
        mapping[chunk] = {
            "module": top[2],
            "score": round(top[0], 3),
            "matched": top[1],
            "wanted": len(named),
        }
        # 并列判据：分差 <0.02 且命中数相同 —— 说明证据不足以唯一确定
        if len(scored) > 1 and scored[1][0] >= top[0] - 0.02 and scored[1][1] == top[1]:
            ambiguous[chunk] = [s[2] for s in scored[:3]]

    with open(out_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump({"root": root, "mapping": mapping, "ambiguous": ambiguous},
                  f, ensure_ascii=False, indent=1)
    return files, imports_by_chunk, mapping, ambiguous


def main(argv):
    sys.stdout.reconfigure(encoding="utf-8")
    root = find_root()
    out_path = DEFAULT_OUT
    queries = [a for a in argv if not a.startswith("-")]

    files, imports_by_chunk, mapping, ambiguous = build(root, out_path)

    print(f"root: {root}  ({len(files)} modules)")
    print(f"chunks with named imports: {len(imports_by_chunk)}")
    print(f"mapped: {len(mapping)}")
    print(f"ambiguous: {len(ambiguous)}")
    print(f"wrote: {out_path}")

    # 自检：分数应为 1.0 全命中，目标模块不应重复
    scores = {v["score"] for v in mapping.values()}
    dupes = len(mapping) - len({v["module"] for v in mapping.values()})
    print(f"distinct scores: {sorted(scores)}   duplicate targets: {dupes}")
    if ambiguous:
        print("ambiguous chunks:")
        for c, cands in ambiguous.items():
            print(f"  {c} -> {cands}")

    for q in queries:
        # 允许省略 .js 后缀
        looks = [q] if q.endswith(".js") else [q + ".js", q]
        hit = next((mapping[k] for k in looks if k in mapping), None)
        print(f"  {q} -> {hit}" if hit else f"  {q} -> NOT MAPPED (可能只被 default import 引用，见 resolve_import.py)")

    return 0 if not ambiguous else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
