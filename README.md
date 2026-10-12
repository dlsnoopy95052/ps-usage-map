# ps-usage-map

Treemap + heatmap view of per-process resource usage on macOS (an Activity Monitor alternative).

- Size and color can each be switched between CPU, Memory, Energy, Threads, Mach Ports, Net In/Out, Disk Read/Write, Sockets
- Tooltip shows listening ports and TCP/UDP connections per process (own-user processes without root)
- Group by app, filter by name, pause/resume (Space)
- Stdlib-only Python server (uses `top` and `ps`) + a single-page Canvas frontend

## Run

```bash
python3 server.py            # http://127.0.0.1:8765
PORT=9000 python3 server.py  # custom port
```

Requires macOS and Python 3.8+. No dependencies.
