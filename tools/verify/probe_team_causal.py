"""同时在页面里记录「收到了哪些 SSE 帧」和「DOM 侧栏状态」，看清因果。

## 为什么要这么做

同一个探针跑两次，一次 PASS 一次 FAIL —— 说明是**竞态**，
不能靠运气判断。必须同时看到两边的时序：

- 前端**收到了**哪些 team_event（在 fetch 层 hook，不依赖 DOM）
- 同一时刻 TeamPanel 的 DOM 是什么

## 做法

用 CDP 的 `Page.addScriptToEvaluateOnNewDocument` 在页面脚本执行**之前**
注入一个 fetch 包装器，把 SSE 响应体 tee 一份出来逐帧解析，
记录到 `window.__frames`。这样拿到的就是前端真正收到的东西。
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from probe_frontend_load import find_chrome, ws_connect, ws_recv, ws_send  # noqa: E402

BASE = "http://127.0.0.1:8080"

# 在页面任何脚本之前注入：包一层 fetch，tee 出 SSE 响应体
INJECT = r"""
(() => {
  window.__frames = [];
  window.__t0 = null;
  const origFetch = window.fetch;
  window.fetch = async function(input, init) {
    const url = typeof input === 'string' ? input : (input && input.url) || '';
    const resp = await origFetch.apply(this, arguments);
    const ct = resp.headers.get('content-type') || '';
    if (!ct.includes('text/event-stream')) return resp;
    if (window.__t0 === null) window.__t0 = Date.now();
    const tag = url.replace(location.origin, '') + '#' + Math.random().toString(36).slice(2,6);
    window.__reqs = window.__reqs || [];
    window.__reqs.push(tag);
    const clone = resp.clone();
    (async () => {
      try {
        const reader = clone.body.getReader();
        const dec = new TextDecoder();
        let acc = '';
        while (true) {
          const {done, value} = await reader.read();
          if (done) break;
          acc += dec.decode(value, {stream:true});
          let i;
          while ((i = acc.indexOf('\n\n')) >= 0) {
            const blk = acc.slice(0, i); acc = acc.slice(i+2);
            const em = blk.match(/^event: (.+)$/m);
            const dm = blk.match(/^data: (.+)$/m);
            if (em) {
              let t = em[1];
              if (t === 'team_event' && dm) {
                try { t = 'team_event:' + JSON.parse(dm[1]).type; } catch(e){}
              }
              window.__frames.push({ms: Date.now() - window.__t0, ev: t, tag: tag});
            }
          }
        }
      } catch (e) {}
    })();
    return resp;
  };
  return 'fetch hooked';
})()
"""

SAMPLER = r"""
(() => {
  window.__samples = [];
  if (window.__timer) clearInterval(window.__timer);
  window.__timer = setInterval(() => {
    const tail = document.querySelector('.team-starting-tail');
    const tasks = document.querySelectorAll('.team-task').length;
    const statusEl = document.querySelector('.team-status');
    const t0 = window.__t0;
    window.__samples.push({
      ms: t0 ? Date.now() - t0 : -1,
      starting: !!tail,
      tasks: tasks,
      status: statusEl ? statusEl.textContent.trim() : null,
    });
  }, 200);
  return 'sampler on';
})()
"""


def main() -> int:
    chrome = find_chrome()
    profile = tempfile.mkdtemp(prefix="hlz_causal_")
    port = 9338
    proc = subprocess.Popen([
        chrome, "--headless=new", f"--remote-debugging-port={port}",
        f"--user-data-dir={profile}", "--no-first-run", "--no-default-browser-check",
        "--disable-gpu", "--disable-dev-shm-usage", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    try:
        target = None
        for _ in range(80):
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/json", timeout=1) as r:
                    tabs = json.load(r)
                target = next((t for t in tabs if t.get("type") == "page"), None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not target:
            print("!! devtools 没起来")
            return 2

        sock, buf = ws_connect(target["webSocketDebuggerUrl"])
        mid = 0

        def call(method, params=None):
            nonlocal mid
            mid += 1
            ws_send(sock, json.dumps({"id": mid, "method": method, "params": params or {}}))
            return mid

        def ev(expr, timeout=60.0):
            nonlocal buf, mid
            mid += 1
            myid = mid
            ws_send(sock, json.dumps({
                "id": myid, "method": "Runtime.evaluate",
                "params": {"expression": expr, "returnByValue": True, "awaitPromise": True},
            }))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw, buf = ws_recv(sock, buf)
                except Exception:
                    return None
                e = json.loads(raw)
                if e.get("id") == myid:
                    return e.get("result", {}).get("result", {}).get("value")
            return None

        call("Page.enable")
        call("Runtime.enable")
        # 关键：在任何页面脚本之前注入
        call("Page.addScriptToEvaluateOnNewDocument", {"source": INJECT})

        call("Page.navigate", {"url": BASE})
        time.sleep(4)

        login = r"""
        (async () => {
          const r = await fetch('/api/auth/login', {
            method:'POST', headers:{'Content-Type':'application/json'},
            body: JSON.stringify({username:'hx', password:'12345678'})
          });
          const d = await r.json();
          localStorage.setItem('app_token', d.access_token);
          return d.access_token ? 'ok' : 'fail';
        })()
        """
        print("[i] login:", ev(login))
        call("Page.navigate", {"url": BASE})
        time.sleep(4)
        print("[i] hook 在吗:", ev("typeof window.__frames"))
        print("[i]", ev(SAMPLER))

        print("[i] new chat:", ev(r"""(() => {
          const b=[...document.querySelectorAll('.side-nav-item')].find(x=>x.textContent.includes('新建'));
          if(!b) return 'no-new'; b.click(); return 'clicked'; })()"""))
        time.sleep(2)
        print("[i] open panel:", ev(
            r"""(() => {const b=document.querySelector('.team-panel-rail'); if(b){b.click();return 'ok'} return 'no-rail'})()"""))
        time.sleep(1)

        print("[i] send:", ev(r"""
        (async () => {
          const ta = document.querySelector('textarea');
          if (!ta) return 'no-textarea';
          const setter = Object.getOwnPropertyDescriptor(
              window.HTMLTextAreaElement.prototype,'value').set;
          setter.call(ta, 'Create an Agent Team with 2 subagents for a simple 3-step demo. Be brief.');
          ta.dispatchEvent(new Event('input',{bubbles:true}));
          await new Promise(r=>setTimeout(r,400));
          const btn = document.querySelector('.send-icon:not(.stop)');
          if (!btn) return 'no-btn';
          if (btn.disabled) return 'disabled';
          btn.click();
          return 'sent';
        })()"""))

        time.sleep(30)
        out = ev(r"""(() => {
          clearInterval(window.__timer);
          const f = (window.__frames||[]).filter(x => x.ev.startsWith('team_event') || x.ev==='done' || x.ev==='message_start');
          const s = window.__samples || [];
          return JSON.stringify({frames: f.slice(0,25), nframes: (window.__frames||[]).length,
                                reqs: window.__reqs || [],
                                samples: s.filter((_,i)=>i%5===0).slice(0,20)});
        })()""")
        d = json.loads(out)
        print()
        print("=" * 66)
        print("前端真实收到的关键帧:")
        for fr in d["frames"]:
            print(f"  +{fr['ms']:6d}ms  {fr['ev']}  [{fr.get('tag','')}]")
        print()
        print("页面发起的 SSE 请求:", d.get("reqs"))
        print()
        print("DOM 侧栏采样（每 1s）:")
        for s in d["samples"]:
            print(f"  +{s['ms']:6d}ms  starting={s['starting']}  tasks={s['tasks']}  status={s['status']}")
        print("=" * 66)

        got_start = any(fr["ev"] == "team_event:team_start" for fr in d["frames"])
        got_task = any(fr["ev"] == "team_event:task_created" for fr in d["frames"])
        last = d["samples"][-1] if d["samples"] else {}
        print()
        print("收到 team_start:", got_start)
        print("收到 task_created:", got_task)
        print("最后一次采样 tasks:", last.get("tasks"), "starting:", last.get("starting"))
        # 判定只看**用户可见结果**：任务板有没有出现、有没有卡在「正在启动」。
        #
        # 不要求 team_start 必须到达 —— 续看路径（/stream?after=0）本来就不重放
        # 历史 team_event，那是设计如此。前端现在能从 task_created 自举出 team，
        # 所以缺 team_start 也能正常渲染。把它写成硬断言会让"已修好"报 FAIL。
        ok = (last.get("tasks") or 0) > 0 and not last.get("starting")
        print("RESULT:", "PASS" if ok else "FAIL")
        if ok:
            print(f"  PASS: 任务板显示 {last.get('tasks')} 个任务，没有卡在「正在启动」")
            print(f"        （team_start 是否到达: {got_start} —— 续看路径不发它属正常）")
        else:
            print("  FAIL: 侧栏仍卡住或任务板为空")
        return 0 if ok else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())