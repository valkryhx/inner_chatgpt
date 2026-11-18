"""验证胖镜像让模型真正用上了新装的命令和包。

换镜像的唯一目的就是这个，所以必须走真实 LLM + 真实 `code_run`，
不能用「镜像里有这个二进制」代替 —— 那只能证明镜像内容，
不能证明模型在沙箱里调得动。

判定用哨兵：让模型把每个命令的输出原样复述。
只要有一条命令在容器里不存在，模型就会报告失败。
"""
from __future__ import annotations

import asyncio
import json
import secrets
import sys
import time
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))

import httpx  # noqa: E402

BASE = "http://127.0.0.1:8000"
HX = "u_c56b8337dd09"
RUNS = Path(__file__).resolve().parents[1] / "e2e" / "sandbox_runs"


async def token_for(uid: str) -> str:
    from app import db
    from app.auth import create_access_token
    u = await db.get_user(uid)
    t, _ = create_access_token(uid, u.get("token_ver", 0))
    return t


def text_of(events: list[dict]) -> str:
    return "".join(e.get("text", "") for e in events if "text" in e)


PROMPT = """请用 code_run 工具（code_type=sh，一次执行）跑下面这段 shell，
然后把**完整原始输出**复述给我。如果某个命令不存在，如实说哪个不存在。

```
echo "S1:$(ps --version 2>&1 | head -1)"
echo "S2:$(curl --version 2>&1 | head -1)"
echo "S3:$(git --version 2>&1)"
echo "S4:$(jq --version 2>&1)"
echo "S5:$(unzip -v 2>&1 | head -1)"
echo "S6:$(rg --version 2>&1 | head -1)"
echo "SENTINEL={sentinel}"
```

然后再用 code_type=python 跑这段，同样复述输出：

```python
import pandas, numpy, openpyxl, docx, pptx, matplotlib, tabulate, bs4, lxml, requests
print("PY_IMPORTS_OK")
df = pandas.DataFrame({{"a": [1, 2, 3]}})
print("PANDAS_SUM", int(df["a"].sum()))
print("SENTINEL={sentinel}")
```
"""

EXPECT = [
    ("ps 可用", "S1:"),
    ("curl 可用", "S2:curl"),
    ("git 可用", "S3:git version"),
    ("jq 可用", "S4:jq-"),
    ("unzip 可用", "S5:"),
    ("rg 可用", "S6:ripgrep"),
    ("10 个 pip 包全部可导入", "PY_IMPORTS_OK"),
    ("pandas 能算", "PANDAS_SUM 6"),
]


async def main() -> int:
    run_id = f"{int(time.time())}-{secrets.token_hex(2)}"
    out = RUNS / run_id
    out.mkdir(parents=True, exist_ok=True)
    sentinel = secrets.token_hex(4)

    token = await token_for(HX)
    headers = {"Authorization": f"Bearer {token}", "Accept": "text/event-stream"}

    async with httpx.AsyncClient(base_url=BASE, timeout=None) as client:
        r = await client.post("/api/sessions", json={"title": f"fatimg-{run_id}"},
                              headers={"Authorization": f"Bearer {token}"})
        r.raise_for_status()
        sid = r.json()["id"]

        events: list[dict] = []
        async with client.stream("POST", f"/api/sessions/{sid}/messages",
                                 json={"text": PROMPT.format(sentinel=sentinel)},
                                 headers=headers, timeout=300) as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                raw = line[6:].strip()
                if raw in ("", "[DONE]"):
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    pass

    (out / "events.jsonl").write_text(
        "\n".join(json.dumps(e, ensure_ascii=False) for e in events), encoding="utf-8")

    # 工具结果里才有真实输出（模型复述可能概括，不可靠）
    tool_out = []
    for e in events:
        if e.get("name") and "content" in e:
            c = e["content"]
            tool_out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    blob = "\n".join(tool_out)
    text = text_of(events)

    (out / "summary.json").write_text(json.dumps({
        "run_id": run_id, "session_id": sid, "sentinel": sentinel,
        "tools": [e["name"] for e in events if e.get("name") and "input" in e],
        "tool_output": blob, "model_text": text,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"run={run_id} sentinel={sentinel}")
    print(f"工具输出长度 {len(blob)}")
    for line in blob.splitlines():
        if line.startswith(("S1:", "S2:", "S3:", "S4:", "S5:", "S6:", "PY_IMPORTS", "PANDAS_SUM")):
            print(f"  {line[:110]}")

    print("\n== 判定（以工具输出为准，不以模型复述为准）==")
    bad = 0
    for name, needle in EXPECT:
        ok = needle in blob
        if not ok:
            bad += 1
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} — 找 {needle!r}")
    ok_sentinel = sentinel in blob
    print(f"  [{'PASS' if ok_sentinel else 'FAIL'}] 哨兵出现在工具输出里")

    total = len(EXPECT) + 1
    print(f"\n== {total-bad-(0 if ok_sentinel else 1)}/{total} 通过 ==")
    print(f"现场：{out}")
    return 0 if (bad == 0 and ok_sentinel) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))