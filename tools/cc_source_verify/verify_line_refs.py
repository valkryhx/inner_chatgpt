"""机器校验文档里引用的行号是否真的落在那句话上。

为什么需要它：记忆机制复核时，v2 有 63 处行号引用，人工核对过一次；
但 v3 又在 v2 基础上加了新的引用。一旦「行号引对、结论引错」这类错误混进来，
下游照抄就会做错事（见 docs/20261113-CC源码结论核验方法论.md）。
这个脚本把每条引用变成可执行断言，重跑一次 87 项，秒级。

数据在 checks.json（模块文件、行号、该行必须包含的片段）。
片段匹配用「子串」而非正则，避免转义坑；需要正则时片段写 `re:` 前缀。

用法：
    python tools/cc_source_verify/verify_line_refs.py
    python tools/cc_source_verify/verify_line_refs.py -v          # 列出每条
    python tools/cc_source_verify/verify_line_refs.py --checks other.json
    CC_DEMINIFIED_ROOT=/path/to/deminified python tools/...

退出码 0 = 全部命中；1 = 有失配（会打印期望与实际）。
"""
import json
import os
import re
import sys

DEFAULT_ROOT = r"D:\git_repos\claude_hack\reconstructed319\deminified"
DEFAULT_CHECKS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "checks.json")


def load_checks(path):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    out = []
    for item in raw["checks"]:
        out.append((item["file"], item["line"], item["must_contain"]))
    return raw, out


def matches(text, needle):
    if needle.startswith("re:"):
        return re.search(needle[3:], text) is not None
    return needle in text


def main(argv):
    root = os.environ.get("CC_DEMINIFIED_ROOT") or DEFAULT_ROOT
    checks_path = DEFAULT_CHECKS
    verbose = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-v", "--verbose"):
            verbose = True
            i += 1
            continue
        if a in ("--checks",):
            checks_path = argv[i + 1]
            i += 2
            continue
        raise SystemExit(f"未知参数 {a!r}")
        i += 1

    meta, checks = load_checks(checks_path)
    sys.stdout.reconfigure(encoding="utf-8")

    cache = {}
    bad = []
    for fn, ln, needle in checks:
        if fn not in cache:
            path = os.path.join(root, fn)
            if not os.path.exists(path):
                cache[fn] = None
            else:
                cache[fn] = open(path, encoding="utf-8").readlines()
        lines = cache[fn]
        if lines is None:
            bad.append((fn, ln, needle, "文件不存在", None))
            continue
        if ln > len(lines):
            bad.append((fn, ln, needle, "行号越界", f"该文件只有 {len(lines)} 行"))
            continue
        text = lines[ln - 1]
        if matches(text, needle):
            if verbose:
                print(f"  OK   {fn}:{ln}  {needle}")
        else:
            bad.append((fn, ln, needle, "未命中", text.strip()[:160]))

    print(f"checks: {checks_path}")
    print(f"root:   {root}")
    print(f"checked {len(checks)}, failed {len(bad)}")
    if meta.get("note"):
        print(f"note:   {meta['note']}")
    for fn, ln, needle, why, actual in bad:
        print(f"  FAIL {fn}:{ln} [{why}] expected: {needle}")
        if actual:
            print(f"       actual: {actual}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
