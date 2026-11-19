# Hlaudz 沙箱服务镜像
#
# 方案：docs/20261119-沙箱服务化方案-bwrap版.md §3.1、§8
#
# 与其他镜像的关系（三份清单必须同步）：
#   backend/requirements.txt        —— app 运行时
#   backend/sandbox/Dockerfile      —— 旧的 sandbox 容器（Docker 方案，保留作回滚）
#   backend/sandbox_service/Dockerfile —— 本文件（bwrap 方案）
#
# ⚠️ 包清单必须与 backend/requirements.txt 的能力一致。
# 漂移的后果是「一开沙箱就坏」：现在沙箱关着跑 app 镜像，一打开同一任务就因缺包失败。
# tests/test_image_dependencies.py 会校验。
#
# 与 backend/sandbox/Dockerfile 的关键差别：
#   1. 装 bubblewrap（不是 docker CLI / SDK）
#   2. **跑非 root**（uid 1000）——这一条是硬要求，不是优化：
#      实测 RLIMIT_NPROC 在 root 下静默失效（起满 60 个进程），非 root 才拦得住
#      （拦在第 18 个）。防 fork 炸弹靠它。
#   3. 不挂 docker.sock（这是 bwrap 方案相对 Docker 方案的核心收益）
#
# ---- 三个 stage（顺序不能改，见文件末尾）----
#
#   base        服务本体（= 生产镜像的全部能力）
#   test        base + pytest（跑 test_bwrap_daemon.py 里 requires_bwrap 那 8 条）
#   production  空 stage，只引用 base；**必须是文件里最后一个**
#
# 为什么 production 必须最后：`docker build` 与 `docker compose build`
# 在不指定 `--target` 时构建**最后一个** stage。若 test 放在最后，
# 一次普通的 `docker compose build sandbox` 就会把 pytest 打进生产镜像——
# 而这只会在 `pip list` 里多一行，日志、启动、隔离全都看着正常。
# `tests/test_sandbox_image_targets.py` 静态钉住这个顺序。

FROM python:3.12-slim AS base

# ---- 国内源 ---------------------------------------------------------------
RUN sed -i 's|http://deb.debian.org|https://mirrors.tuna.tsinghua.edu.cn|g' \
        /etc/apt/sources.list.d/debian.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        # ★ bwrap 本身
        bubblewrap \
        # prlimit（资源限制）。bwrap 自己不做限制，只能外挂。
        util-linux \
        # 中文字体：画图必需。不装时中文渲染成方块**且不报错**，用户拿到废图。
        fontconfig fonts-noto-cjk \
        # 模型常用的命令
        git curl wget ca-certificates jq less \
        procps file tree ripgrep unzip zip \
    && rm -rf /var/lib/apt/lists/* \
    && fc-cache -fv > /dev/null 2>&1 || true

# ---- Python 依赖 -----------------------------------------------------------
# 与 backend/requirements.txt 保持一致（分组见 backend/sandbox/Dockerfile 的注释）
ENV PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

RUN pip install --no-cache-dir \
        requests pandas numpy scipy \
        openpyxl xlrd tabulate \
        python-docx python-pptx reportlab jinja2 \
        matplotlib pillow beautifulsoup4 lxml

# ---- 服务自身依赖 ----------------------------------------------------------
# fastapi/uvicorn 是控制面的，与用户代码的运行时（上面那批）分开装，
# 便于将来把运行时换成独立 rootfs（方案 §4.7 待决策 2）。
RUN pip install --no-cache-dir "fastapi>=0.115" "uvicorn[standard]>=0.32" httpx

# ---- 运行时环境 ------------------------------------------------------------
# 与 executor/bwrap.py 的 --setenv 保持一致（双保险：镜像里也设一份，
# 这样即使有人直接 docker exec 进来跑 python 也有正确的环境）。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MPLCONFIGDIR=/tmp/mpl \
    TZ=Asia/Shanghai \
    LANG=C.UTF-8

# ---- 非 root 用户 ----------------------------------------------------------
# ★ 关键：bwrap 的 --uid 是「假降权」（userns 内映射，内核视角仍是 0），
#   所以 RLIMIT_NPROC 在 bwrap 里设了也没用。必须让**容器本身**非 root。
#   实测：uid 1000 下 nproc=20 拦在第 18 个；root 下起满 60 个。
RUN useradd --create-home --uid 1000 sandbox \
    && mkdir -p /app /workspaces \
    && chown -R sandbox:sandbox /app /workspaces

WORKDIR /app

# 只拷服务代码，不拷整个 backend（app 的业务代码不该进这个镜像）
COPY --chown=sandbox:sandbox backend/sandbox_service/ /app/sandbox_service/

USER sandbox

EXPOSE 8021

# 健康检查用 python urllib（镜像里不保证有 curl 也不该为它多装）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8021/v1/health', timeout=4)"

# 默认 bwrap 执行器。--no-access-log 减少噪音（每次 exec 都会打一条）。
CMD ["uvicorn", "sandbox_service.main:app", "--host", "0.0.0.0", "--port", "8021"]


# ============================================================================
# test stage：base + pytest
# ============================================================================
#
# 为什么需要它：`tests/test_bwrap_daemon.py` 里 8 条 `requires_bwrap` 测试
# 在 Windows 上全部 skip（没有 bwrap），而生产镜像按设计不带 pytest。
# 于是这 8 条测试**在任何环境都不会执行** —— 偏偏它们覆盖的正是
# Windows 上根本测不到的部分：进程组清理、PID namespace、bwrap fork 内层进程。
#
# 实测踩到过：`_Daemon.kill()` 只杀外层 bwrap，内层被 init 收养继续活着
# （`docs/probes/20261119-daemon进程清理探针.py` 抓到 `VERDICT: LEAK`）。
# 这类 bug 日志完全看不出来，只有跑真进程测试才能发现。
#
# 构建与运行：
#
#   docker build -f backend/sandbox_service/Dockerfile \
#                --target test -t hlz-sandbox-test:latest .
#
#   docker run --rm \
#     -v "$PWD/backend/tests:/app/tests:ro" \
#     --user 1000:1000 \
#     hlz-sandbox-test:latest \
#     python -m pytest /app/tests/test_bwrap_daemon.py -q
#
# ⚠️ 必须 `--user 1000:1000`：本镜像默认 USER 是 sandbox(1000)，但若调用方
#    镜像重建时忘了，root 下 `RLIMIT_NPROC` 会静默失效，测出来的隔离结论是假的。
#
# ⚠️ 测试文件**挂载**进去，不 COPY 进镜像：`.dockerignore` 已经排除
#    `backend/tests/`，且测试不该成为镜像的一部分（镜像要能被拿去跑用户代码）。
FROM base AS test
USER root
RUN pip install --no-cache-dir pytest
USER sandbox


# ============================================================================
# production stage：**必须是文件里最后一个**（见文件头说明）
# ============================================================================
#
# 空 stage：它不添加任何东西，只是给「生产镜像」一个可以指名的落点。
#
# 存在的唯一目的是让「默认构建 = 生产镜像」成为结构上的保证，
# 而不是靠人记得加 `--target base`。改名 / 挪位置都会让
# `tests/test_sandbox_image_targets.py` 变红。
FROM base AS production