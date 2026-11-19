"""镜像依赖清单：数据/绘图/文档类库必须预装。

## 为什么钉这个（2026-11-19 真机事故）

hx 让 4 个 sub 做「先 2 并行后 2 串行」的示例任务，其中 worker_c 要画图。
它执行 `pip install matplotlib` —— **失败了一次又一次**（19:14/19:15/19:19/
19:21/19:22），每次都卡在慢网络超时，整轮卡了 10 分钟，`turns` 一直是 0。

父代理后来自己诊断出来了：

> 已定位根因：环境里没有 matplotlib、pandas、numpy，只有 PIL 13.0.0，
> 所以 C 一直在反复执行 pip 却装不上。

沙箱镜像的注释早就写明了这条原则：

> 容器是 network_mode="none"，**运行时 pip install 必然失败**。
> 所以模型能用的命令和包必须预先装好，不能"用时再装"。

但这条原则只落实在沙箱镜像里，**app 镜像没有**。而当前
`HLZ_SANDBOX_MODE=off`，code_run 走的是 app 容器自己的 python ——
所以缺包的就是 app 镜像。

## 修复后必须成立

1. app 镜像（requirements.txt）包含数据/绘图/文档类库；
2. 沙箱镜像（sandbox/Dockerfile）包含**同一套**同样能力 —— 两边不能漂移，
   否则打开沙箱后又会踩同一个坑；
3. 两份清单里不能有明显缺失（比如 app 有 pandas 而沙箱没有）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
REQUIREMENTS = BACKEND / "requirements.txt"
SANDBOX_DOCKERFILE = BACKEND / "sandbox" / "Dockerfile"

# 模型做数据分析/画图/生成文档时真正会用到的包。
# 加包时同步加到这里 —— 这是「能力清单」，不是实现细节。
REQUIRED_CAPABILITIES = {
    # 数据处理
    "pandas": "数据处理（读 csv/excel、groupby、透视）",
    "numpy": "数值计算",
    "scipy": "科学计算（统计、插值、拟合）",
    # 绘图
    "matplotlib": "绘图（折线/柱状/散点）",
    "pillow": "图像处理（PIL）",
    # Excel / Word / PPT
    "openpyxl": "读写 .xlsx",
    "xlrd": "读旧版 .xls（openpyxl 不支持）",
    "python-docx": "生成 .docx",
    "python-pptx": "生成 .pptx",
    # 文本/网页解析
    "beautifulsoup4": "HTML 解析（扒网页/表格）",
    # HTTP（模型写脚本时习惯 import requests，不是 httpx）
    "requests": "HTTP 客户端",
    # 报表
    "tabulate": "把表格渲染成 markdown/text",
    "jinja2": "模板渲染（生成报告）",
    "reportlab": "生成 PDF",
}


def _norm(name: str) -> str:
    """包名归一：pip 里大小写/下划线/连字符不敏感。"""
    return name.lower().replace("_", "-")


def requirements_names() -> set[str]:
    """requirements.txt 里声明的包名（已归一，去掉版本约束）。"""
    out: set[str] = set()
    for raw in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        # 去掉 extras 与版本约束：uvicorn[standard]==0.41.0 -> uvicorn
        name = line.split("[", 1)[0]
        for sep in ("==", ">=", "<=", "~=", "!=", ">", "<", " "):
            name = name.split(sep, 1)[0]
        if name:
            out.add(_norm(name))
    return out


def sandbox_pip_names() -> set[str]:
    """沙箱 Dockerfile 里 `pip install` 的包名（已归一）。

    只看 RUN pip install 块 —— 注释里提到包名不算。
    """
    text = SANDBOX_DOCKERFILE.read_text(encoding="utf-8")
    # 去掉续行反斜杠，便于按行解析
    joined = text.replace("\\\n", " ")
    names: set[str] = set()
    for raw in joined.splitlines():
        line = raw.split("#", 1)[0].strip()
        if "pip install" not in line:
            continue
        tail = line.split("pip install", 1)[1]
        # 去掉 pip 的选项（--no-cache-dir / -i <url> / -r <file>）
        tokens = tail.split()
        skip_next = False
        for tok in tokens:
            if skip_next:
                skip_next = False
                continue
            if tok.startswith("-"):
                # -i / --index-url 后面跟一个参数
                if tok in ("-i", "--index-url", "--extra-index-url", "-r", "--requirement"):
                    skip_next = True
                continue
            name = tok.split("[", 1)[0]
            for sep in ("==", ">=", "<=", "~=", "!=", ">", "<"):
                name = name.split(sep, 1)[0]
            if name:
                names.add(_norm(name))
    return names


# ---------------------------------------------------------------- app 镜像

@pytest.mark.parametrize("pkg,why", sorted(REQUIRED_CAPABILITIES.items()))
def test_app_image_has_capability(pkg, why):
    """app 镜像必须预装这个包 —— 缺了模型就会去 pip（慢网络 -> 卡住）。"""
    names = requirements_names()
    assert _norm(pkg) in names, (
        f"requirements.txt 缺 {pkg}（{why}）。\n"
        f"缺包的后果不是报错，而是模型去 pip install 然后卡在超时上"
        f"（2026-11-19 worker_c 卡了 10 分钟）。"
    )


# ---------------------------------------------------------------- 沙箱镜像

@pytest.mark.parametrize("pkg,why", sorted(REQUIRED_CAPABILITIES.items()))
def test_sandbox_image_has_capability(pkg, why):
    """沙箱镜像必须预装同一个包 —— 容器无网络，运行时装不上。"""
    names = sandbox_pip_names()
    assert _norm(pkg) in names, (
        f"sandbox/Dockerfile 缺 {pkg}（{why}）。\n"
        f"沙箱是 network_mode=none，运行时 pip 必然失败，只能预装。"
    )


def test_two_images_do_not_drift():
    """两个镜像的能力清单不能漂移。

    漂移的后果：现在沙箱关着，app 镜像有包所以能跑；一旦打开沙箱，
    同一个任务立刻失败。这类"开关一翻就坏"的配置最难排查。
    """
    req = requirements_names()
    sbx = sandbox_pip_names()
    caps = {_norm(p) for p in REQUIRED_CAPABILITIES}

    only_app = (req & caps) - sbx
    only_sbx = (sbx & caps) - req
    assert not only_app, f"这些包 app 镜像有、沙箱镜像没有：{sorted(only_app)}"
    assert not only_sbx, f"这些包沙箱镜像有、app 镜像没有：{sorted(only_sbx)}"


# ---------------------------------------------------------------- 运行时环境

APP_DOCKERFILE = BACKEND / "deploy" / "Dockerfile"


def test_app_image_sets_mplconfigdir():
    """app 镜像必须设 MPLCONFIGDIR。

    不设的后果（2026-11-19 实测）：matplotlib 想写 /root/.config/matplotlib，
    被 workspace 路径守卫拦下，于是每次 import 都重建临时缓存 ——
    能跑，但慢，而且日志里全是吓人的「写入越界」警告。
    沙箱镜像早就设了 /tmp/.mpl，app 镜像漏了。
    """
    text = APP_DOCKERFILE.read_text(encoding="utf-8")
    assert "MPLCONFIGDIR" in text, (
        "app 镜像没有设 MPLCONFIGDIR —— matplotlib 会尝试写 /root/.config，"
        "被 workspace 守卫拦下后每次 import 重建缓存"
    )


# CJK 字体包的候选名（Debian/Ubuntu 里能提供中文的几种）
_CJK_FONT_PKGS = (
    "fonts-noto-cjk", "fonts-wqy-zenhei", "fonts-wqy-microhei",
    "fonts-arphic", "fonts-noto",
)


def _live_lines(path: Path) -> list[str]:
    """返回非注释行（去掉 `#` 之后的内容也去掉）。

    ⚠️ 必须这样过滤：注释里当然会**提到**字体包名（解释为什么装它），
    用 `pkg in text` 会在注释命中 → 即使真删了 RUN 行也判绿。
    2026-11-19 反向确认时就踩了这个：删了实际安装行，测试仍全绿。
    """
    out = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0]
        if line.strip():
            out.append(line)
    return out


def _installs_cjk_fonts(path: Path) -> bool:
    joined = " ".join(_live_lines(path))
    return any(pkg in joined for pkg in _CJK_FONT_PKGS)


def test_app_image_installs_cjk_fonts():
    """app 镜像必须装中文字体。

    不装的后果（2026-11-19 实测 fc-list 输出 0）：模型画图时写
    `plt.xlabel("月份")` 会渲染成方块 —— 用户拿到一张废图，
    而且没有任何报错，非常难发现。
    """
    assert _installs_cjk_fonts(APP_DOCKERFILE), (
        "app 镜像没装 CJK 字体 —— 中文图表会渲染成方块（实测 fc-list 为 0）"
    )


def test_sandbox_image_installs_cjk_fonts():
    """沙箱镜像同样要装中文字体，不能只修一边。"""
    assert _installs_cjk_fonts(SANDBOX_DOCKERFILE), (
        "sandbox/Dockerfile 没装 CJK 字体，打开沙箱后中文图会是方块"
    )


def test_workspace_env_points_mplconfigdir_inside_workspace(tmp_path):
    """MPLCONFIGDIR 必须落在 workspace 内。

    镜像里设的 `/tmp/.mpl` 对普通进程够用，但 code_run 的子进程被
    WORKSPACE_PYTHON_GUARD 限制成「只能写 workspace」。2026-11-19 实测：

        mkdir -p failed for path /tmp/.mpl: 写入越界

    matplotlib 于是降级到 workspace 内的临时目录 —— 能跑，但每次 import
    都重建缓存，而且日志里全是吓人的越界警告。
    """
    from app.run_guard import WORKSPACE_PYTHON_GUARD, workspace_env

    ws = tmp_path / "user_ws"
    ws.mkdir()
    env = workspace_env(ws)

    mpl = env.get("MPLCONFIGDIR")
    assert mpl, "workspace_env 没设 MPLCONFIGDIR，会用到镜像里那个 /tmp/.mpl"
    from pathlib import Path
    assert Path(mpl).resolve().is_relative_to(ws.resolve()), (
        f"MPLCONFIGDIR={mpl} 不在 workspace {ws} 内，会被写入守卫拦下"
    )
    # 顺带确认其他临时变量也都在 workspace 内（原有契约）
    for key in ("TMP", "TEMP", "TMPDIR"):
        assert Path(env[key]).resolve().is_relative_to(ws.resolve()), key


def test_capability_list_is_not_trivially_empty():
    """防止有人把清单清空让测试全绿（反向确认的守卫）。"""
    assert len(REQUIRED_CAPABILITIES) >= 10
    assert _norm("matplotlib") in {_norm(k) for k in REQUIRED_CAPABILITIES}