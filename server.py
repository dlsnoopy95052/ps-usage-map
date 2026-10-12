#!/usr/bin/env python3
"""ps-usage-map: local server that samples per-process resource usage on macOS
(via `top` + `ps`) and serves a treemap/heatmap web UI. Stdlib only."""
import ctypes
import json
import os
import pty
import re
import struct
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
INTERVAL = 2  # seconds between samples
PORT = int(os.environ.get("PORT", "8765"))

STATS = "pid,command,cpu,mem,threads,ports,power,idlew,state,user"
UNITS = {"B": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}
APP_RE = re.compile(r"([^/]+)\.app/")

_lock = threading.Lock()
_snapshot = {"time": 0, "processes": [], "system": {}}

# --- per-process network rate (nettop) ---
_net_rates = {}   # pid -> (bytes_in/s, bytes_out/s)
_net_lock = threading.Lock()

# --- per-process disk I/O (libproc proc_pid_rusage, own-user processes only) ---
_libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
_RUSAGE_INFO_V4 = 4
_rbuf = (ctypes.c_uint8 * 512)()
_disk_prev = {}   # pid -> (read_bytes, write_bytes, time)


def disk_rate(pid, now):
    """Return (read B/s, write B/s) since previous sample, or (0, 0)."""
    if _libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V4, _rbuf) != 0:
        return 0.0, 0.0
    # ri_uuid[16] then 18 uint64 fields; diskio read/written are the last two
    v = struct.unpack_from("<16s18Q", bytes(_rbuf))
    rd, wr = v[17], v[18]
    prev = _disk_prev.get(pid)
    _disk_prev[pid] = (rd, wr, now)
    if not prev or now <= prev[2] or rd < prev[0] or wr < prev[1]:
        return 0.0, 0.0
    dt = now - prev[2]
    return (rd - prev[0]) / dt, (wr - prev[1]) / dt


# --- per-process sockets (lsof): listening ports and connections ---
_sockets = {}   # pid -> {"sockets": n, "listen": [...], "conns": [...]}
_sock_lock = threading.Lock()
MAX_LIST = 40


def parse_lsof(text):
    out, pid, rec = {}, None, None

    def flush():
        if pid is None or not rec or "proto" not in rec:
            return
        d = out.setdefault(pid, {"sockets": 0, "listen": [], "conns": []})
        d["sockets"] += 1
        name, proto, state = rec.get("name", ""), rec["proto"], rec.get("state", "")
        if "->" in name:
            item = f"{proto} {name}" + (f" {state}" if state else "")
            if item not in d["conns"]:
                d["conns"].append(item)
        elif name and not name.endswith(":*"):
            if proto == "TCP" and state not in ("LISTEN", ""):
                return
            item = f"{proto} {name}"
            if item not in d["listen"]:
                d["listen"].append(item)

    for line in text.splitlines():
        if not line:
            continue
        tag, val = line[0], line[1:]
        if tag == "p":
            flush()
            pid, rec = int(val), None
        elif tag == "f":
            flush()
            rec = {}
        elif rec is not None:
            if tag == "P":
                rec["proto"] = val
            elif tag == "n":
                rec["name"] = val
            elif tag == "T" and val.startswith("ST="):
                rec["state"] = val[3:]
    flush()
    return out


def socket_sampler():
    import time as _t
    while True:
        try:
            text = subprocess.run(["lsof", "-nP", "-i", "-FpPnT"], capture_output=True,
                                  text=True, errors="replace").stdout
            data = parse_lsof(text)
            for d in data.values():
                d["listen"] = sorted(d["listen"])[:MAX_LIST]
                d["conns"] = d["conns"][:MAX_LIST]
            with _sock_lock:
                _sockets.clear()
                _sockets.update(data)
        except Exception:
            pass
        _t.sleep(INTERVAL)


def net_sampler():
    """Stream `nettop` and keep latest per-pid in/out byte rates."""
    cmd = ["nettop", "-P", "-L", "0", "-s", str(INTERVAL), "-J", "bytes_in,bytes_out", "-x"]
    # nettop block-buffers when stdout is a pipe; a pty makes it flush per line
    master, slave = pty.openpty()
    subprocess.Popen(cmd, stdout=slave, stderr=subprocess.DEVNULL)
    os.close(slave)
    prev, cur, prev_t, cur_t = {}, {}, 0.0, 0.0
    for line in os.fdopen(master, "r", buffering=1, errors="replace"):
        line = line.rstrip("\r\n")
        if line.startswith(",bytes_in"):  # header marks a new sample block
            if cur:
                if prev and cur_t > prev_t:
                    dt = cur_t - prev_t
                    rates = {}
                    for pid, (i, o) in cur.items():
                        pi, po = prev.get(pid, (i, o))
                        rates[pid] = (max(i - pi, 0) / dt, max(o - po, 0) / dt)
                    with _net_lock:
                        _net_rates.clear()
                        _net_rates.update(rates)
                prev, prev_t = cur, cur_t
            cur, cur_t = {}, time.time()
            continue
        f = line.split(",")
        if len(f) < 3:
            continue
        name_pid = f[0].rsplit(".", 1)
        if len(name_pid) == 2 and name_pid[1].isdigit():
            try:
                cur[int(name_pid[1])] = (float(f[1] or 0), float(f[2] or 0))
            except ValueError:
                pass


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
    now = time.time()
    with _net_lock:
        net = dict(_net_rates)
    with _sock_lock:
        socks = dict(_sockets)
    live = {r["pid"] for r in rows}
    for pid in [p for p in _disk_prev if p not in live]:
        del _disk_prev[pid]
    procs = []
    for r in rows:
        r["net_in"], r["net_out"] = net.get(r["pid"], (0.0, 0.0))
        r["disk_read"], r["disk_write"] = disk_rate(r["pid"], now)
        sk = socks.get(r["pid"], {})
        r["sockets"] = sk.get("sockets", 0)
        r["listen"] = sk.get("listen", [])
        r["conns"] = sk.get("conns", [])
        path = paths.get(r["pid"], "")
        name = os.path.basename(path) if path else r["command"]
        # outermost .app bundle is the owning application
        apps = APP_RE.findall(path)
        app = apps[0] if apps else name
        procs.append({**r, "name": name or r["command"], "app": app, "path": path})
    return {"time": now, "processes": procs, "system": system}


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
    threading.Thread(target=net_sampler, daemon=True).start()
    threading.Thread(target=socket_sampler, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"ps-usage-map running at http://127.0.0.1:{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
