#!/usr/bin/env python3
"""把 tutorials/ 编译成可直接发布的静态站点。

不使用 Jekyll：交互图是自包含 HTML（单文件数百 KB，内含全部 JS/CSS/字体），
经 Jekyll 处理只会引入 Liquid 冲突风险；这里只做一件 Jekyll 本来该做的事——
把 Markdown 渲染成带样式的独立 HTML，其余文件原样复制。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import markdown

CSS = """
:root{--bg:#faf7f2;--fg:#2b2521;--muted:#7a6f66;--line:#e5ded4;--accent:#c4633f;--code:#f3ece4}
@media (prefers-color-scheme:dark){:root{--bg:#16130f;--fg:#ece5dc;--muted:#9b8f83;--line:#332c25;--accent:#e08a63;--code:#231e18}}
*{box-sizing:border-box}
body{margin:0;padding:44px 20px 84px;background:var(--bg);color:var(--fg);
     font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC",sans-serif;
     line-height:1.78;font-size:15.5px}
.wrap{max-width:760px;margin:0 auto}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:26px;line-height:1.32;margin:0 0 20px;letter-spacing:-.012em}
h2{font-size:19px;margin:40px 0 12px;padding-bottom:7px;border-bottom:1px solid var(--line)}
h3{font-size:16px;margin:26px 0 10px}
p{margin:0 0 14px}ul,ol{margin:0 0 14px;padding-left:24px}li{margin-bottom:7px}
code{background:var(--code);padding:2px 6px;border-radius:5px;
     font-family:ui-monospace,"SF Mono","Cascadia Code",Menlo,monospace;font-size:.89em}
pre{background:var(--code);padding:16px 18px;border-radius:8px;overflow-x:auto;
    margin:0 0 16px;line-height:1.55;border:1px solid var(--line)}
pre code{background:none;padding:0;font-size:13px}
blockquote{margin:0 0 16px;padding:2px 0 2px 16px;border-left:3px solid var(--accent);
           color:var(--muted)}
blockquote p{margin:0}
table{border-collapse:collapse;width:100%;margin:0 0 18px;font-size:14px}
th,td{text-align:left;padding:9px 12px;border:1px solid var(--line);vertical-align:top}
th{background:var(--code);font-weight:600}
hr{border:0;border-top:1px solid var(--line);margin:36px 0}
.backlink{display:inline-block;margin-bottom:26px;font-size:13.5px;color:var(--muted)}
""".strip()

TEMPLATE = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>{css}</style>
</head>
<body><div class="wrap">
<a class="backlink" href="./">&larr; 返回教程目录</a>
{body}
</div></body></html>
"""


def first_heading(text: str) -> str | None:
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return None


def build(root: Path, out: Path) -> int:
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 1

    out.mkdir(parents=True, exist_ok=True)
    rendered = 0
    copied = 0

    for md in sorted(root.glob("*.md")):
        text = md.read_text(encoding="utf-8")
        body = markdown.markdown(text, extensions=["tables", "fenced_code"])
        title = first_heading(text) or md.stem
        html = TEMPLATE.format(title=title, css=CSS, body=body)
        target = out / (md.stem + ".html")
        target.write_text(html, encoding="utf-8", newline="\n")
        print(f"rendered {md.name} -> {target.name}")
        rendered += 1

    for item in sorted(root.iterdir()):
        if not item.is_file() or item.suffix == ".md":
            continue
        if item.name.startswith("_") or item.name.startswith("."):
            continue
        # archify 的校验回执等中间产物不发布
        if item.name.endswith((".finalize.json", ".finalize-summary.json",
                               ".delivery.json", ".browser-check.json",
                               ".visual-check.json")):
            continue
        shutil.copy2(item, out / item.name)
        print(f"copied   {item.name}")
        copied += 1

    print(f"done: {rendered} rendered, {copied} copied -> {out}")
    return 0


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("tutorials")
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("_site")
    raise SystemExit(build(src, dst))