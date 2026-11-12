"""严格核实：湛卢 deepseek 的思考模式是否真的可关，以及延迟差异是否真实。

上一轮结论的三个薄弱点，逐一验证：
  1. enable_thinking=false 到底被网关接受了，还是被静默忽略？
     （如果被忽略，"变快"就只是噪声 —— 这是致命的）
  2. 默认是否真的在"思考"？用足够大的 max_tokens 看 reasoning_tokens 与 reasoning_content
  3. 延迟差异是否显著？用充足样本 + 交错取样，而不是 5-8 次就下结论
"""
from __future__ import annotations

import json
import socket
import ssl
import statistics
import sys
import time
from pathlib import Path

HOST = "ecloud.10086.cn"
PATH = "/api/query/aigateway/v1/chat/completions"
MODEL = "moma/deepseek-latest-flash"


def load_key() -> str:
    env = Path(__file__).resolve().parents[1] / ".env"
    for line in env.read_text(encoding="utf-8").splitlines():
        if line.strip().startswith("ZhanLu_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise SystemExit("no key")


def post(payload: dict, stream: bool = True, timeout: int = 120):
    """底层发一次请求，返回 (status, 原始响应体, 分段计时)。"""
    key = load_key()
    body = json.dumps(payload).encode()
    ctx = ssl.create_default_context()
    t0 = time.perf_counter()
    sock = ctx.wrap_socket(socket.create_connection((HOST, 443), timeout=30),
                           server_hostname=HOST)
    t_conn = time.perf_counter() - t0
    sock.sendall(
        (f"POST {PATH} HTTP/1.1\r\nHost: {HOST}\r\n"
         f"Authorization: Bearer {key}\r\nContent-Type: application/json\r\n"
         f"Content-Length: {len(body)}\r\n"
         f"Accept: {'text/event-stream' if stream else 'application/json'}\r\n"
         f"Connection: close\r\n\r\n").encode() + body
    )
    raw = b""
    first_byte = None
    while True:
        d = sock.recv(65536)
        if not d:
            break
        if first_byte is None:
            first_byte = time.perf_counter() - t0
        raw += d
    total = time.perf_counter() - t0
    sock.close()
    head, _, payload_bytes = raw.partition(b"\r\n\r\n")
    status_line = head.split(b"\r\n")[0].decode("latin-1", "replace")
    return status_line, payload_bytes, {"conn": t_conn, "first_byte": first_byte, "total": total}


def parse_stream(payload_bytes: bytes) -> dict:
    """从 SSE 里抽出：正文、思考内容、usage、首个正文时间差（用字节数近似无用，这里只看内容）。"""
    content, reasoning = [], []
    usage = None
    err = None
    for line in payload_bytes.decode("utf-8", "replace").splitlines():
        if not line.startswith("data:"):
            continue
        p = line[5:].strip()
        if p in ("", "[DONE]"):
            continue
        try:
            ev = json.loads(p)
        except Exception:
            continue
        if "error" in ev:
            err = ev["error"]
        if ev.get("usage"):
            usage = ev["usage"]
        ch = (ev.get("choices") or [{}])[0]
        dl = ch.get("delta") or {}
        if dl.get("content"):
            content.append(dl["content"])
        rc = dl.get("reasoning_content") or dl.get("reasoning")
        if rc:
            reasoning.append(rc)
    return {
        "content": "".join(content),
        "reasoning": "".join(reasoning),
        "usage": usage,
        "error": err,
    }


def expr1_is_thinking_actually_on():
    """核实 1：默认到底在不在思考？用足够大的 max_tokens，看 reasoning 字段。"""
    print("=" * 74)
    print("核实 1：默认模式下模型是否真的产生 reasoning（max_tokens=800，足够大）")
    print("=" * 74)
    for label, extra in [
        ("默认（不传任何思考参数）", {}),
        ("enable_thinking=false", {"enable_thinking": False}),
    ]:
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "9.11 和 9.9 哪个大？说明理由。"}],
            "max_tokens": 800,
            "temperature": 1.0,
            "stream": True,
            **extra,
        }
        status, body, timing = post(payload)
        r = parse_stream(body)
        u = r["usage"] or {}
        det = u.get("completion_tokens_details") or {}
        print(f"\n  [{label}]  {status}")
        if r["error"]:
            print(f"    !! 网关报错: {json.dumps(r['error'], ensure_ascii=False)[:200]}")
            continue
        print(f"    reasoning_content 长度: {len(r['reasoning'])} 字符")
        print(f"    completion_tokens={u.get('completion_tokens')}  "
              f"reasoning_tokens={det.get('reasoning_tokens')}  "
              f"text_tokens={det.get('text_tokens')}")
        print(f"    正文({len(r['content'])} 字符): {r['content'][:70]!r}")
        if r["reasoning"]:
            print(f"    思考({len(r['reasoning'])} 字符): {r['reasoning'][:100]!r}")


def expr2_param_accepted():
    """核实 2：网关是否真的认 enable_thinking，还是静默忽略？"""
    print("\n" + "=" * 74)
    print("核实 2：网关对 enable_thinking 的接受度（看 usage 差异，而不是看快慢）")
    print("=" * 74)
    print("  判据：若被静默忽略，两种配置的 reasoning_tokens 应无系统性差异；")
    print("        若被接受，false 时 reasoning_tokens 应恒为 0。\n")

    for label, extra in [("默认", {}), ("false", {"enable_thinking": False})]:
        rts = []
        for i in range(5):
            payload = {
                "model": MODEL,
                "messages": [{"role": "user",
                              "content": "一个笼子里有鸡和兔共 35 只，脚共 94 只。各几只？只给答案。"}],
                "max_tokens": 800, "temperature": 1.0, "stream": True, **extra,
            }
            status, body, _ = post(payload)
            r = parse_stream(body)
            if r["error"]:
                rts.append(f"ERR:{json.dumps(r['error'], ensure_ascii=False)[:60]}")
                continue
            det = (r["usage"] or {}).get("completion_tokens_details") or {}
            rts.append(det.get("reasoning_tokens"))
            print(f"    {label} #{i+1}: reasoning_tokens={det.get('reasoning_tokens')} "
                  f"正文={(r['content'] or '')[:30]!r} 思考长={len(r['reasoning'])}")
        print(f"  -> {label}: {rts}\n")


def expr3_latency_significance():
    """核实 3：延迟差异是否显著（交错取样 12 轮，配对比较）。"""
    print("=" * 74)
    print("核实 3：延迟差异显著性（交错取样 12 轮，同一时段交替发）")
    print("=" * 74)
    a_list, b_list = [], []
    for i in range(12):
        for tag, extra, bucket in [
            ("A默认", {}, a_list),
            ("B关思考", {"enable_thinking": False}, b_list),
        ]:
            payload = {
                "model": MODEL,
                "messages": [{"role": "user", "content": "只回复两个字：好的"}],
                "max_tokens": 100, "temperature": 1.0, "stream": True, **extra,
            }
            status, body, timing = post(payload)
            fb = timing["first_byte"]
            r = parse_stream(body)
            if r["error"]:
                print(f"  #{i+1} {tag}: 错误 {json.dumps(r['error'], ensure_ascii=False)[:80]}")
                continue
            ms = fb * 1000 if fb else None
            if ms:
                bucket.append(ms)
            print(f"  #{i+1:2d} {tag:8s} 首字节 {round(ms) if ms else '-':>6} ms  "
                  f"总 {round(timing['total']*1000):>6} ms  "
                  f"正文={(r['content'] or '')[:10]!r}")

    print()
    for name, v in [("默认", a_list), ("enable_thinking=false", b_list)]:
        if not v:
            continue
        v_sorted = sorted(v)
        p50 = statistics.median(v)
        p90 = v_sorted[min(len(v_sorted) - 1, int(len(v_sorted) * 0.9))]
        print(f"  {name:24s} n={len(v):2d}  中位 {p50:7.0f}  "
              f"均值 {statistics.mean(v):7.0f}  最小 {min(v):6.0f}  "
              f"P90 {p90:7.0f}  最大 {max(v):7.0f}")

    if a_list and b_list:
        paired = list(zip(a_list, b_list))
        diffs = [a - b for a, b in paired[: min(len(a_list), len(b_list))]]
        wins = sum(1 for d in diffs if d > 0)
        print(f"\n  配对比较（A默认 - B关思考）：{wins}/{len(diffs)} 轮 A 更慢")
        print(f"  差值中位 {statistics.median(diffs):.0f} ms")
        if wins >= len(diffs) * 0.75:
            print("  -> 关思考确实更快，差异方向一致")
        elif wins <= len(diffs) * 0.25:
            print("  -> 关思考反而更慢，此前结论要推翻")
        else:
            print("  -> 方向不一致，不能断定关思考更快")


if __name__ == "__main__":
    expr1_is_thinking_actually_on()
    expr2_param_accepted()
    expr3_latency_significance()