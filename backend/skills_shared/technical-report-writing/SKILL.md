---
name: technical-report-writing
description: 规范公司内部技术调研报告、技术评估报告和阶段性技术总结的写作、引用、Word 字体与版式交付。Use when writing, revising, polishing, validating, or formatting Chinese technical reports, especially .docx reports for leadership review, reports needing restrained engineering style, superscript citations, 仿宋 font solidification, or first-line indentation.
---
# 技术报告撰写

## Quick Start

Use this skill when the user asks to write, revise, polish, validate, or format a Chinese internal technical report.

Default output style:

- Audience: internal technical leaders, architects, R&D managers, decision makers.
- Tone: senior engineer or architect summary; plain, restrained, objective.
- Goal: explain facts, basis, engineering impact, cost, risk, and decision advice.
- Avoid: marketing language, AI-like filler, exaggerated claims, unsupported absolute judgments.
- Word delivery: `.docx` with citation superscripts, 仿宋 typography, and body first-line indentation.

## Writing Rules

- State claims with boundaries: use “在当前公开数据下”, “在该评测集上”, “可能”, “通常”, “需要进一步验证”.
- Replace subjective praise with measurable descriptions, engineering impact, or concrete risks.
- Do not infer all-scenario conclusions from one benchmark or a single source.
- Avoid repeated Chinese-English bracket stacking. Use common Chinese terms or common English abbreviations directly.
- Keep conclusions tied to engineering decisions: trial scope, evaluation set, deployment constraints, budget, governance.

Forbidden or high-risk wording:

- Marketing: “史诗级”, “震撼”, “革命性”, “重磅”, “颠覆性”, “降维打击”, “不可思议”, “终极”, “核武器”, “未来已来”, “遥遥领先”.
- Absolutes: “绝对”, “完全”, “彻底”, “必然”, “唯一”, “所有场景”, “毫无疑问”, “无可替代”.
- AI filler: “总而言之”, “不可否认”, “在这个飞速发展的时代”, “这标志着一个新时代的到来”.

## Report Structure

Use this structure unless the user provides a stronger template:

1. 标题: direct object and scope.
2. 背景与范围: time range, technology scope, source types, analysis goal.
3. 核心技术分析: release/version context, architecture, metrics, cost/resources, scenarios, risks.
4. 横向对比: table with stable comparable fields.
5. 部署与成本评估: API cost, local hardware, memory/network/storage, latency/throughput, security/ops.
6. 结论与建议: decision-oriented, not slogan-like.
7. 信源或参考资料: numbered, traceable, matched by body citations.

## Citation Rules

- Body citations use `[x]`, displayed as true superscript in Word.
- Citation text must include brackets, e.g. `[40]`, not bare `40`.
- Put citation before punctuation: `成本为每百万 Token 1.25 美元[18]。`
- Multiple sources can be adjacent: `[12][13]`.
- Do not merge citations into versions, prices, percentages, or dates.
- Source list entries remain normal text, not superscript.

## Word Formatting

Standard Word format:

- Body paragraphs: 仿宋, 11 pt, first-line indent 22 pt.
- Heading 1: 仿宋, 24 pt, bold.
- Heading 2: 仿宋, 18 pt, bold.
- Heading 3: 仿宋, 14 pt, bold.
- Heading 4: 仿宋, 12 pt, bold.
- Tables: 仿宋, 11 pt; header row bold; no first-line indent in cells.
- Citations: 仿宋, `[x]`, true superscript, usually 8 pt.
- Do not implement indentation by manually inserting spaces or tabs. Use Word paragraph first-line indentation.

When formatting `.docx`, prefer the bundled script:

```powershell
python C:\Users\drago\.codex\skills\technical-report-writing\scripts\format_docx.py "input.docx" "output.docx"
```

The script solidifies fonts in styles, runs, `word/document.xml`, and `word/styles.xml`.

## Verification

Before claiming the report is ready, verify:

- No obvious marketing, exaggerated, lyrical, or unsupported absolute wording remains.
- Key data and claims have traceable sources.
- Body/table citations are true superscript `[x]`.
- There are no bare superscript digits.
- Citations appear before punctuation.
- Source list numbers match body citations and are normal text.
- Body paragraphs have 22 pt first-line indent; headings, tables, and source list do not.
- Fonts are固化为仿宋 across headings, body, tables, and superscript citations.

For detailed style requirements, see [REFERENCE.md](REFERENCE.md).
