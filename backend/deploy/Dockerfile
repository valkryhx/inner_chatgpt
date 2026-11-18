# 后端镜像。
#
# 只装「跑后端」需要的东西：沙箱镜像（backend/sandbox/Dockerfile）是
# 另一回事 —— 那是给用户代码用的，跟这个镜像无关。
FROM python:3.12-slim

# 时区与中文：DB 里存的是毫秒时间戳，但日志/文件名会用本地时间。
ENV TZ=Asia/Shanghai \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 清华源：国内构建速度差一个数量级。
# 用 `sed` 而不是 ADD sources.list，避免多一层。
RUN sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's|deb.debian.org|mirrors.tuna.tsinghua.edu.cn|g' /etc/apt/sources.list

# git：部分 skill / MCP 工具需要；tini：让 SIGTERM 正确传给 uvicorn
# （否则容器停止时 lifespan shutdown 不跑，实例不从心跳表注销）。
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates tini \
    && rm -rf /var/lib/apt/lists/*

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