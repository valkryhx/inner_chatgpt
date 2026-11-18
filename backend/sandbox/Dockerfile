# Hlaudz 沙箱镜像（MVP）
#
# 设计文档：docs/20261118-沙箱MVP设计与实施.md §6.3、§11
#
# 为什么需要这个镜像，而不是直接用 python:3.12-slim：
#   容器是 network_mode="none"，**运行时 pip install 必然失败**。
#   所以模型能用的命令和包必须预先装好，不能"用时再装"。
#
# 实测数据（2026-11-18，本机 Docker Desktop / Windows）：
#   python:3.12-slim       177 MB   创建 263ms   exec 73~94ms   pandas 无
#   本镜像                  733 MB   创建 624ms   exec 72~81ms   pandas 680ms
#   → exec 延迟没有变差，代价在体积和创建耗时。
#
# 构建（约 2 分钟，实测 54s apt + 67s pip，用国内源）：
#   docker build -t hlz-sandbox:2026-11-18 backend/sandbox/
#
# 改包后记得同步更新 hlz.sandbox.image 环境变量与本文档 §11 的实测表。
#
# 为什么不用 alpine：musl libc，numpy/pandas/matplotlib 的官方 wheel 是
# manylinux（glibc），在 alpine 上要么无 wheel 要么现场编译（慢且易失败）。

FROM python:3.12-slim

# ---- 国内源 ----------------------------------------------------------------
# deb.debian.org 实测 94.5 kB/s（apt-get update 要 1分47秒），换清华源后
# 整个 apt 阶段 54 秒。trixie = Debian 13（python:3.12-slim 当前基线）。
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        # procps: ps/top/pgrep/pkill。
        # ⚠️ 本项目的杀进程**不依赖**它（走 /proc，见 runtime.py 的说明），
        # 但模型自己会想用 ps 看进程，缺了会很难排查。
        procps \
        # 网络工具：模型常写 curl 验证接口 / wget 下文件
        curl wget ca-certificates \
        # 版本控制与打包
        git unzip zip \
        # JSON 处理：API 响应加工几乎必用
        jq \
        # 文本查看
        less vim-tiny \
        # 文件类型与目录浏览
        file tree \
        # 搜索。find/grep 已有，ripgrep 对模型更友好（.gitignore 感知、更快）
        ripgrep \
    && rm -rf /var/lib/apt/lists/* \
    # vim-tiny 只提供 `vi`，但模型习惯写 `vim`；补个别名避免它以为编辑器不存在
    && ln -sf /usr/bin/vi /usr/local/bin/vim

# ---- Python 依赖 -----------------------------------------------------------
# 全部预装。容器无网络，运行时安装不可能。
# 分组说明（后续要裁剪就按组裁）：
#   报表/数据处理：pandas numpy openpyxl tabulate
#   文档生成：python-docx python-pptx
#   HTTP 与解析：requests beautifulsoup4 lxml
#   绘图：matplotlib
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir \
        requests \
        pandas \
        numpy \
        openpyxl \
        tabulate \
        python-docx \
        python-pptx \
        matplotlib \
        beautifulsoup4 \
        lxml

# ---- 运行时环境 ------------------------------------------------------------
# 与 runtime.py 的 environment 保持一致（那里也设了这几项，双保险）。
# HOME=/tmp 是因为 /tmp 是 tmpfs，容器销毁即消失，不写入 /ws。
ENV HOME=/tmp \
    TMPDIR=/tmp \
    XDG_CACHE_HOME=/tmp/.cache \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/.mpl

WORKDIR /ws

# 不用 ENTRYPOINT：runtime.py 用 `sleep infinity` 常驻 + exec_run 逐次执行，
# 容器本身不该有默认命令行为。
CMD ["sleep", "infinity"]