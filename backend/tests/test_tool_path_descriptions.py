"""工具描述里的「路径怎么写」必须是完整可执行的指引。

## 背景（真实故障，2026-11-18 用户自测「写一个 8 皇后」发现）

模型第一次跑 `code_run` 时经常猜错文件路径。根因不是模型笨，是**没人告诉它**：

1. 文件类工具的 path 描述只说「workspace 相对路径」，但**没说 workspace 根是什么**、
   也没说绝对路径行不行 —— 模型只能试；
2. `file_patch` 的 path 描述**干脆是空的**（实测 `description: ''`）；
3. `code_run` 的工作目录就是 workspace 根，但描述里一个字都没提；
4. 更麻烦的是沙箱：容器内 cwd 是 `/ws`，宿主是 `D:\\sandbox\\<uid>`，
   模型如果按宿主路径写死，换个运行位置就崩。

## 这里锁的契约

每个涉及文件的工具，`path` 参数必须有**非空**描述，且说明：
- 相对于 workspace 根（不是相对于某个抽象概念）；
- 可以直接给相对路径；
- 不要拼宿主绝对路径。
"""
from __future__ import annotations

import pytest

from app import builtin_tools

# 所有接受 path 参数的内置工具
PATH_TOOLS = ("file_read", "file_write", "file_patch",
              "file_glob", "file_grep", "list_files")


def schemas(uid: str = "u_x") -> dict[str, object]:
    return {t.name: t for t in builtin_tools.build_builtin_tools(uid, include_memory=False)}


@pytest.mark.parametrize("tool_name", PATH_TOOLS)
def test_path_param_has_description(tool_name):
    """path 参数不能是空描述 —— 空描述等于让模型猜。"""
    t = schemas()[tool_name]
    props = t.input_schema.get("properties", {})
    assert "path" in props, f"{tool_name} 没有 path 参数"
    desc = (props["path"].get("description") or "").strip()
    assert desc, f"{tool_name}.path 的描述是空的，模型只能猜"


@pytest.mark.parametrize("tool_name", PATH_TOOLS)
def test_path_description_mentions_relative(tool_name):
    """必须明确说「相对 workspace 根」，而不只是「路径」。"""
    t = schemas()[tool_name]
    desc = (t.input_schema["properties"]["path"].get("description") or "")
    combined = desc + " " + (t.description or "")
    low = combined.lower()
    assert ("相对" in combined or "relative" in low
            or "workspace 根" in combined
            or "当前目录" in combined), \
        f"{tool_name} 没说路径相对谁：{desc!r}"


def test_code_run_description_mentions_cwd():
    """code_run 的 cwd 就是 workspace 根 —— 必须告诉模型，否则它拼绝对路径。"""
    t = schemas()["code_run"]
    desc = t.description or ""
    assert any(k in desc for k in ("当前目录", "工作目录", "cwd", "相对路径")), \
        f"code_run 没说工作目录在哪：{desc[:150]!r}"


def test_no_tool_description_tells_model_to_use_host_absolute_path():
    """不能有任何描述**鼓励**用宿主绝对路径（那会在沙箱里崩）。

    ⚠️ 注意区分：说「不要拼 C:\\ 绝对路径」是**正确**的；
    说「用 C:\\... 这样的绝对路径」才是错的。所以这里检查的是
    「出现宿主路径且没带否定词」，不是简单禁词。
    """
    negatives = ("不要", "禁止", "别", "勿", "don't", "never", "avoid", "而不是")
    for name, t in schemas().items():
        blob = (t.description or "") + str(t.input_schema)
        for host in ("D:\\", "C:\\", "/home/", "/Users/"):
            if host not in blob:
                continue
            # 找到出现位置，看前后 40 字符有没有否定词
            idx = blob.find(host)
            ctx = blob[max(0, idx - 40): idx + 40]
            assert any(n in ctx for n in negatives), (
                f"{name} 的工具描述提到宿主路径 {host} 但没带否定语境，"
                f"会把模型带偏：…{ctx}…")