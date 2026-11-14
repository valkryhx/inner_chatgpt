# CC 源码调研核验工具

这组脚本用于**核对文档里关于 Claude Code 源码的断言**。它们不是在读代码，而是在验证
「文档说的那句话，在二进制里到底是不是那样」。

配套文档：

- `docs/20261113-CC源码结论核验方法论.md` —— 为什么需要这套工具（长期有效）
- `docs/20261114-CC319记忆机制源码复核v3.md` —— 用这套工具做出的最新复核
- `docs/20261113-CC319记忆机制源码核实v2.md` —— 被复核的对象

## 前置：源码树不在仓库里

CC 2.1.319 是 Bun 单文件打包，**整个 50.9 MiB JS 都在 `claude.exe` 的 `.bun` 段里**，
仓库没有也不该有它。需要先把 `.bun` 段拆开、按 `// @bun @bytecode` 标记切成模块并 deminify，
得到：

```
<CC_DEMINIFIED_ROOT>/
    m0000_0x84910b4.js
    m0002_0x1120cbb8.js
    ...                            共 2,882 个
```

本机实测路径是 `D:\git_repos\claude_hack\reconstructed319\deminified`（74 MB）。
换机器用环境变量覆盖，脚本里没有第二个硬编码：

```bash
export CC_DEMINIFIED_ROOT=/path/to/deminified
```

二进制基线（复现时先核对，否则行号对不上）：SHA256 `2E055732…45CBD`、
GIT_SHA `fbdd7e02…8f2`、BUILD_TIME `2026-11-05T18:14:18Z`。

## 脚本清单

| 脚本 | 干什么 | 什么时候跑 |
|---|---|---|
| `verify_line_refs.py` | 把文档里每条 `文件:行` 引用变成可执行断言，重跑 87 项 | **改完文档必跑** |
| `build_chunk_map.py` | 把 `chunk-xxx.js` 还原成 `mNNNN` 模块名（1,764 条） | 文档引用了新 chunk 时 |
| `resolve_import.py` | 查某个符号从哪个 chunk 导入（反查） | 读源码时定位符号来源 |
| `dump_refs.py` | 按行号区间导出源码原文成对照表 | 要逐行读某个区域时 |

```bash
python tools/cc_source_verify/verify_line_refs.py          # 退出码 0 = 全命中
python tools/cc_source_verify/verify_line_refs.py -v       # 逐条列出

python tools/cc_source_verify/build_chunk_map.py
python tools/cc_source_verify/build_chunk_map.py chunk-m1zmpfya

python tools/cc_source_verify/resolve_import.py m0293_0x1177df9f.js Pv yg jf
python tools/cc_source_verify/resolve_import.py m0477_0x11b1de8e.js n0n

python tools/cc_source_verify/dump_refs.py                 # 导出内置 v3 引用清单
python tools/cc_source_verify/dump_refs.py m0477:15:18     # 自定义区间
python tools/cc_source_verify/dump_refs.py -o work/dump.txt
```

## 最有价值的一件事：chunk 从来不是文件

**这是读 CC 打包产物时最容易踩的坑。**

反编译出的 2,882 个模块之间这样互相引用：

```js
import { Pv, yg, jf } from "B:/~BUN/root/chunk-m1zmpfya.js";
```

但 `B:/~BUN/root/chunk-m1zmpfya.js` **在磁盘上不存在**，`B:/~BUN/root/` 是 Bun 的虚拟根，
这些只是原始模块图里的 specifier。所以文档里凡是写「某 chunk 里如何如何」的，
都还没落到能看的源码上 —— 必须先还原成 `mNNNN_0x………….js`。

### 怎么还原（`build_chunk_map.py` 的原理）

deminify 会改文件名、改本地标识符，但**不会改跨模块导出的名字**。于是：

> 对某个 chunk C，所有模块从 C 导入的**具名符号的并集**，等于真实模块 C 的导出列表。

按「交集 / 导入数」给每个候选模块打分，取最高且 ≥0.6 的。本机实测：

```
chunks with named imports: 1764
mapped: 1764
ambiguous: 0
distinct scores: [1.0]        ← 全部满分匹配
duplicate targets: 0          ← 没有两个 chunk 指向同一模块
```

分数全是 1.0、无重复目标、无并列，说明这个还原是**确定的**而非启发式猜的。

四个已知答案的交叉验证（先知道答案再验方法）：

| chunk | 应还原成 | 依据 |
|---|---|---|
| `chunk-m1zmpfya.js` | `m0290` | 导入 `Pv`/`yg`/`jf`/`Nji` 四个符号，`m0290` 正好导出这四个（4/4） |
| `chunk-27h4st9h.js` | `m0591` | 3,655 个符号全命中 |
| `chunk-drx9cnw1.js` | `m0003` | 15/15 |
| `chunk-agg547ae.js` | `m0004` | 1/1 |

**只有 `default` import 的 chunk 无法用符号集区分**，脚本会跳过（1,764 条里没出现这种情况，
因为所有被 import 的 chunk 都至少有一个具名符号）。真遇到时用 `resolve_import.py` 人工看。

### 方法论上的意义

`docs/20261113-CC源码结论核验方法论.md` 里有一条：「**import 图不是证据**」——
共享 chunk 被 1,263 个模块引用，看到 `A import B` 不能推出「B 里那个函数被 A 调用」。
`build_chunk_map.py` 不违反这条，它是**反向**用的：不是用 import 关系推行为，
而是用导出符号集**定位文件**。定位之后仍然要回源码读调用点、实参、下游消费者。

## 为什么 `verify_line_refs.py` 值得长期留着

复核 v2 时抓到的 6 处错误里，**只有 2 处是行号错位**，其余 4 处是
「行号引对了、但结论读错了」——引用的那行确实在那儿，可它不是
v2 说的那个意思（比如把 `memory_read` 的 schema 当成 `memory_list` 的）。

所以这个脚本只防行号漂移，**防不了误读**。它的价值在于：改文档、改源码树之后，
能一秒确认「引用的位置还在不在」，把复核的注意力集中在解释上，而不是浪费在
数行号上（v2 的 63 个区间、2,625 行，人工数一遍要很久）。

`checks.json` 里每条是 `{file, line, must_contain}`；`must_contain` 用子串匹配，
`re:` 前缀走正则（只在必须用正则时用，避免转义坑——`m0084:26` 那条
`e.replace(/\[(1|2)m\]/gi, "")` 就这么踩过一次）。

加新引用时直接往 `checks.json` 里加一条，不用改代码。

## 目录里还有什么

| 文件 | 说明 |
|---|---|
| `checks.json` | 87 条行号断言，`verify_line_refs.py` 的数据 |
| `chunk_map.json` | 1,764 条 chunk→模块映射（204 KB，纯文本，可入库） |

`chunk_map.json` 入库的理由：源码树不在仓库里，没有它就无法在别的机器上把
文档里的 chunk 名还原成模块名。它是纯查表数据、无密钥、体积可接受。

## 跑不通时

| 现象 | 原因 |
|---|---|
| `没有在 … 找到 m*.js` | `CC_DEMINIFIED_ROOT` 不对 |
| 行号大面积失配 | 源码树不是 2.1.319（先核对 SHA256 / BUILD_TIME） |
| `chunk-xxx -> NOT MAPPED` | 后缀写错（脚本会同时试带/不带 `.js`），或该 chunk 只被 default import 引用 |
| 中文输出乱码 | 脚本已 `reconfigure(encoding="utf-8")`；若是 PowerShell 侧乱码，`chcp 65001` |
