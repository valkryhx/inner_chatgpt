import subprocess, sys, time, urllib.request, socket
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

port = free_port()
boot = ROOT / "work" / "_mw_boot.py"
boot.write_text(
    "import uvicorn\n"
    "if __name__ == '__main__':\n"
    f"    uvicorn.run('app.main:app', host='127.0.0.1', port={port}, workers=2, reload=False, log_level='info')\n",
    encoding="utf-8", newline="\n")
print("starting 2 workers on", port)
p = subprocess.Popen([sys.executable, str(boot)], cwd=str(ROOT/"backend"),
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
ok = False
for _ in range(60):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
            if r.status == 200:
                ok = True; break
    except Exception:
        time.sleep(0.5)
print("healthy:", ok)
time.sleep(2)
p.terminate()
try: out = p.communicate(timeout=10)[0].decode("utf-8","replace")
except subprocess.TimeoutExpired:
    p.kill(); out = p.communicate()[0].decode("utf-8","replace")
print("--- workers spawned? ---")
print("Started parent process" in out or "Started server process" in out)
import re
print("process ids:", re.findall(r"Started server process \[(\d+)\]", out))
print(out[-800:])
