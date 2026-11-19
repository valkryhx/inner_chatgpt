# 后端镜像。
#
# 只装「跑后端」需要的东西：沙箱镜像（backend/sandbox/Dockerfile）是
# 另一回事 —— 那是给用户代码用的，跟这个镜像无关。
FROM python:3.12-slim

# 时区与中文：DB 里存的是毫秒时间戳，但日志/文件名会用本地时间。
#
# MPLCONFIGDIR：matplotlib 的缓存目录。**必须指到可写位置**，
# 否则它会去写 /root/.config/matplotlib —— 而 code_run 的 workspace
# 路径守卫会拦下这次写入（日志里是「写入越界」），然后它降级到
# workspace 内的临时目录，每次 import 都重建缓存（实测日志见
# work/verify_deps.py 的输出）。指到 /tmp 后一次建立、后续复用。
# 与 backend/sandbox/Dockerfile 保持一致。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    MPLCONFIGDIR=/tmp/.mpl

# 清华源：国内构建速度差一个数量级。
# 用 `sed` 而不是 ADD sources.list，避免多一层。
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list

# git：部分 skill / MCP 工具需要；tini：让 SIGTERM 正确传给 uvicorn
# （否则容器停止时 lifespan shutdown 不跑，实例不从心跳表注销）。
#
# fonts-noto-cjk：**中文字体，画图必需**（2026-11-19 实测发现）。
# 不装的后果：模型写 `plt.xlabel("月份")` 会渲染成方块 —— 用户拿到一张废图，
# 而且没有任何报错，极难发现。基础镜像 python:3.12-slim 里 fc-list 为 0。
#
# fontconfig：matplotlib 靠它发现字体（没装的话 fc-list / font_manager
# 都看不到装好的字体）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tini \
        fontconfig fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*
# 刷新字体缓存：装完不刷的话 matplotlib 的 fontManager 仍可能读不到新字体。
RUN fc-cache -fv > /dev/null 2>&1 || true

WORKDIR /app/backend

COPY backend/requirements.txt ./requirements.txt
RUN pip install -i https://pypi.tuna.tsinghua.edu.cn/simple \
        -r requirements.txt

COPY backend/ ./

# data 目录：SQLite 模式下是库文件位置，PG 模式下放记忆/工作区/日志。
# 多实例时应该挂共享存储或改用对象存储（见 docs）。
RUN mkdir -p /app/backend/data

EXPOSE 8000

# tini 作为 PID 1：容器收到 SIGTERM 时能正确转发给 python，
# uvicorn 才会跑 lifespan shutdown（注销实例、销毁沙箱容器）。
ENTRYPOINT ["/usr/bin/tini", "--"]

# 不用 --reload，不用 --workers：多副本靠 compose/K8s 横向扩，
# 单容器内多 worker 会让「一个实例」这个概念失效（心跳/owner 是进程级的）。
CMD ["python", "-m", "uvicorn", "app.main:app", \
     "--host", "0.0.0.0", "--port", "8000"]