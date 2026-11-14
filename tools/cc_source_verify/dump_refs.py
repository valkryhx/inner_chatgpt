"""按行号区间导出 deminified 源码原文，用于逐行核对文档里的引用。

用途：文档里写了「`m0591:151656` 是 `var yIe = null`」，复核时要把那段原文摊开逐行读。
一条 `sed -n` 只能看一处，这个脚本一次导出几十处，输出成对照表。

用法：
    # 导出内置的 v3 引用清单（对应 docs/20261114-…复核v3.md）
    python tools/cc_source_verify/dump_refs.py
    # 自定义区间：模块前缀:起:止，可给多个
    python tools/cc_source_verify/dump_refs.py m0591:151656:151660 m0477:15:18
    # 指定输出文件（默认打屏）
    python tools/cc_source_verify/dump_refs.py -o work/dump.txt

模块前缀就写 `m0591`（`_0x...` 后缀省略），脚本自己找对应文件。
见 docs/20261114-CC319记忆机制源码复核v3.md §0。
"""
import os
import sys

DEFAULT_ROOT = r"D:\git_repos\claude_hack\reconstructed319\deminified"

# v3 复核时实际导出的关键区间（模块前缀, 起, 止）。值在于「哪些地方值得摊开看」。
V3_REFS = [
    # 投递通道：instructions attachment vs system prompt
    ("m0848", 1181, 1200), ("m0848", 1225, 1235), ("m0848", 1280, 1295),
    ("m0848", 1440, 1450), ("m0848", 1850, 1860),
    ("m0477", 10, 20), ("m0191", 16350, 16375),
    ("m0591", 229030, 229040), ("m0591", 10975, 10982),
    # 工具面：memory_list / memory_read
    ("m0816", 2950, 2995), ("m0591", 75825, 75855),
    # 索引预算与召回预算的单位
    ("m0293", 4470, 4485), ("m0591", 74470, 74480), ("m0591", 74505, 74512),
    # 拼装器归属：Dl / Ll / li / jl
    ("m0293", 4195, 4210), ("m0293", 4390, 4420), ("m0293", 4560, 4580),
    ("m0293", 4735, 4790),
    # 中立化标记集：fwt / Cae / j / Yx
    ("m0051", 115, 125), ("m0182", 40, 65),
    # 远端能力与 surfaceCapabilities
    ("m0095", 50, 60), ("m0040", 6285, 6295), ("m0040", 1878, 1890),
    # 账号记忆链（yIe 恒 null）
    ("m0591", 151650, 151660), ("m0591", 153840, 153945),
    # 组织记忆闸门
    ("m0293", 415, 440), ("m0293", 655, 670), ("m0293", 2870, 2880),
    # 环境变量与 SDK 入口判定
    ("m0064", 875, 890), ("m0066", 185, 195),
]


def find_module(root, prefix):
    for name in os.listdir(root):
        if name.startswith(prefix + "_") and name.endswith(".js"):
            return name
    return None


def dump(root, refs, out_path=None):
    lines_out = []
    for pre, a, b in refs:
        fn = find_module(root, pre)
        if not fn:
            lines_out.append(f"### {pre} MISSING")
            continue
        with open(os.path.join(root, fn), encoding="utf-8") as f:
            lines = f.readlines()
        lines_out.append(f"### {fn} [{a}-{b}] total={len(lines)}")
        for i in range(a, min(b, len(lines)) + 1):
            lines_out.append(f"{i}: {lines[i - 1].rstrip()[:500]}")
        lines_out.append("")

    text = "\n".join(lines_out)
    if out_path:
        with open(out_path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        print(f"wrote {out_path} ({len(lines_out)} lines)")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return len(lines_out)


def parse_arg(spec):
    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit(f"区间格式应为 模块前缀:起:止，收到 {spec!r}")
    pre, a, b = parts
    return (pre, int(a), int(b))


def main(argv):
    root = os.environ.get("CC_DEMINIFIED_ROOT") or DEFAULT_ROOT
    out_path = None
    refs = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-o", "--out"):
            out_path = argv[i + 1]
            i += 2
            continue
        refs.append(parse_arg(a))
        i += 1
    dump(root, refs or V3_REFS, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
