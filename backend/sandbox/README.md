# 沙箱镜像：构建 / 分发 / 版本

设计文档：`docs/20261118-沙箱MVP设计与实施.md`

## 为什么需要自建镜像

容器是 `network_mode="none"`，**运行时 `pip install` 必然失败**。
模型能用的命令和 Python 包必须**预先装进镜像**，不能"用时再装"。

直接用 `python:3.12-slim`（177 MB）的问题：实测缺 `ps` / `pkill` / `curl` /
`wget` / `git` / `unzip` / `jq` / `less` / `vim`，模型在 `code_run` 里写这些命令
会直接失败，且失败原因对模型不透明（它以为是环境坏了）。

## 实测对比（2026-11-18，本机 Docker Desktop / Windows）

| 镜像 | 体积 | 容器创建 | exec 延迟 | pandas |
| --- | --- | --- | --- | --- |
| `python:3.12-slim` | 177 MB | 263 ms | 73~94 ms | 无 |
| `hlz-sandbox:2026-11-18` | 733 MB | 281~624 ms | 72~109 ms | 680 ms（首次 import） |

**exec 延迟没有变差**，代价在体积和创建耗时。创建耗时在镜像层进入页缓存后
接近 slim（实测 283 ms vs 263 ms）。

首次 import 耗时：pandas 680 ms / matplotlib 895 ms / numpy 295 ms / requests 194 ms。
这些是**一次性**的（同一容器内后续 import 走 `sys.modules`），但每次新容器要重付。

## 构建

```bash
cd backend
docker build -t hlz-sandbox:2026-11-18 sandbox/
```

耗时约 **2 分钟**（apt 阶段 ~54s + pip 阶段 ~67s），用清华源。
`deb.debian.org` 实测 94.5 kB/s，不换源要 3 分钟以上。

**构建必须在能访问外网的环境**（构建期需要网络，运行期不需要）。

### 改包之后

1. 改 `sandbox/Dockerfile`；
2. **换 tag**（用日期或语义版本），不要覆盖旧 tag —— 旧容器还在用旧镜像，
   覆盖会让"回滚"失去对象；
3. 重新构建，更新 `app/sandbox/config.py` 的 `DEFAULT_IMAGE`；
4. 更新本文档的实测表；
5. 跑 `backend/tests/manual/sandbox_acceptance.py` + `sandbox_fat_image_e2e.py`。

## 分发

本 MVP 假设**后端与 Docker 在同一台机器**（本机 Docker Desktop），
镜像只需本地存在。部署到另一台机器时按以下任一方式：

```bash
# 方式 A：导出/导入（离线，最简单）
docker save hlz-sandbox:2026-11-18 -o hlz-sandbox-20261118.tar   # ~720 MB
docker load -i hlz-sandbox-20261118.tar

# 方式 B：推私有 registry
docker tag hlz-sandbox:2026-11-18 <registry>/hlz-sandbox:2026-11-18
docker push <registry>/hlz-sandbox:2026-11-18
```

方式 B 之后建议**固定 digest**（`<registry>/hlz-sandbox@sha256:...`），
避免 tag 被覆盖导致不同机器跑不同镜像 —— 这是 `config.py` 里
「换机器前请按 §9.1-R8 固定 digest」那条注释的实际做法。

## 镜像内容

### 系统命令（apt）

| 包 | 提供的命令 | 为什么需要 |
| --- | --- | --- |
| `procps` | `ps` `top` `pgrep` `pkill` | 模型排查进程。**注意：本项目杀进程不依赖它**（走 `/proc`，见 `runtime.py`），装它只是为了模型自己好用 |
| `curl` `wget` `ca-certificates` | `curl` `wget` | 模型常写 curl 验证接口 / wget 下文件（注意容器无外网，只能访问挂载进来的东西） |
| `git` | `git` | 版本控制 |
| `unzip` `zip` | `unzip` `zip` | 处理上传的压缩包 |
| `jq` | `jq` | API 响应加工几乎必用 |
| `less` `vim-tiny` | `less` `vi`（另加了 `vim` 软链） | 文本查看 |
| `file` `tree` | `file` `tree` | 文件类型判断、目录浏览 |
| `ripgrep` | `rg` | 搜索（`.gitignore` 感知，比 grep 快） |

### Python 包（pip）

| 组 | 包 | 用途 |
| --- | --- | --- |
| 报表/数据 | `pandas` `numpy` `openpyxl` `tabulate` | 数据处理与 Excel 读写 |
| 文档 | `python-docx` `python-pptx` | Word / PPT 生成 |
| 网络与解析 | `requests` `beautifulsoup4` `lxml` | HTTP（内网可达时）与 HTML/XML 解析 |
| 绘图 | `matplotlib` | 图表 |

### 体积构成（供后续裁剪参考）

| 部分 | 约 |
| --- | --- |
| `python:3.12-slim` 基线 | 177 MB |
| apt 系统命令 | +172 MB |
| pip 10 个包 | +384 MB |
| **合计** | **733 MB** |

要缩小，按此顺序：`matplotlib`（含大量字体）→ `python-docx`/`python-pptx` →
`lxml`（编译产物大）→ `git`（含 perl 依赖）。

## 为什么不用 alpine

`alpine` 12 MB 看着诱人，但它是 **musl libc**，而 `numpy` / `pandas` /
`matplotlib` 的官方 wheel 是 manylinux（**glibc**）。在 alpine 上要么没有
预编译 wheel，要么现场编译（构建慢十倍、更易失败、需要 gcc 工具链反而更大）。
装完这些包后 alpine 的体积优势基本消失。

如果用 `python:3.12-alpine`，`pip install pandas` 会尝试从源码编译，
构建时间从分钟级变成十几分钟，且需要额外的 `build-base` 等包。

## 环境变量

镜像里已写死（`Dockerfile` 的 `ENV`），`runtime.py` 创建容器时也传了一份
（双保险，后者优先）：

| 变量 | 值 | 原因 |
| --- | --- | --- |
| `HOME` | `/tmp` | `/tmp` 是 tmpfs，容器销毁即消失；不写 `/ws` |
| `TMPDIR` | `/tmp` | 同上 |
| `XDG_CACHE_HOME` | `/tmp/.cache` | 同上 |
| `MPLCONFIGDIR` | `/tmp/.mpl` | matplotlib 会往 HOME 写字体缓存，必须指到 tmpfs |
| `PYTHONUNBUFFERED` | `1` | 输出不缓冲，否则长跑任务的输出会攒着不返回 |
| `PYTHONDONTWRITEBYTECODE` | `1` | 不生成 `__pycache__`，避免污染用户的 `/ws` |