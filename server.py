#!/usr/bin/env python3
"""ps-usage-map: local server that samples per-process resource usage on macOS
(via `top` + `ps`) and serves a treemap/heatmap web UI. Stdlib only."""
import json
import os
import re
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INTERVAL = 2  # seconds between samples
PORT = int(os.environ.get("PORT", "8765"))

STATS = "pid,command,cpu,mem,threads,ports,power,idlew,state,user"
UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
APP_RE = re.compile(r"([^/]+)\.app/")

_lock = threading.Lock()
_snapshot = {"time": 0, "processes": [], "system": {}}


def num(tok):
    return float(re.sub(r"[^0-9.]", "", tok) or 0)


def mem_bytes(tok):
    m = re.match(r"([0-9.]+)([BKMGT]?)", tok)
    if not m:
        return 0
    return float(m.group(1)) * UNITS.get(m.group(2) or "B", 1)


def ps_paths():
    """pid -> full executable path (top truncates command names)."""
    out = subprocess.run(["ps", "-axo", "pid=,comm="], capture_output=True, text=True).stdout
    paths = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit():
            paths[int(parts[0])] = parts[1]
    return paths


def build(rows, system):
    paths = ps_paths()
    procs = []
    for r in rows:
        path = paths.get(r["pid"], "")
        name = os.path.basename(path) if path else r["command"]
        # outermost .app bundle is the owning application
        apps = APP_RE.findall(path)
        app = apps[0] if apps else name
        procs.append({**r, "name": name or r["command"], "app": app, "path": path})
    return {"time": __import__("time").time(), "processes": procs, "system": system}


def sampler():
    cmd = ["top", "-l", "0", "-s", str(INTERVAL), "-stats", STATS]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True, bufsize=1)
    rows, system, in_table, samples = [], {}, False, 0
    for line in proc.stdout:
        line = line.rstrip("\n")
        if line.startswith("Processes:"):
            if in_table:
                samples += 1
                if samples >= 2:  # first sample's CPU is not a delta; skip it
                    snap = build(rows, system)
                    with _lock:
                        _snapshot.update(snap)
            rows, system, in_table = [], {}, False
        elif line.startswith("PID"):
            in_table = True
        elif in_table:
            t = line.split()
            if len(t) < 10 or not t[0].isdigit():
                continue
            th = t[-6].split("/")[0]
            rows.append({
                "pid": int(t[0]),
                "command": " ".join(t[1:-8]),
                "cpu": num(t[-8]),
                "mem": mem_bytes(t[-7]),
                "threads": num(th),
                "ports": num(t[-5]),
                "power": num(t[-4]),
                "wakeups": num(t[-3]),
                "state": t[-2],
                "user": t[-1],
            })
        elif line.startswith("CPU usage:") or line.startswith("PhysMem:"):
            k, v = line.split(":", 1)
            system[k] = v.strip()
    sys.exit("top exited unexpectedly")


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, body, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/processes"):
            with _lock:
                body = json.dumps(_snapshot).encode()
            self._send(body, "application/json")
        elif self.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                self._send(f.read(), "text/html; charset=utf-8")
        else:
            self.send_error(404)


if __name__ == "__main__":
    threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ps-usage-map running at http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
