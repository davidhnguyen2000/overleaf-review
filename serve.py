#!/usr/bin/env python3
"""Keep the review server up.

    python3 serve.py --pdf root.pdf --out .pdf-review/pending.md [--port 8765]

Prints the URL on the first line, then supervises. The server has been seen
dying to an outside signal mid-session, leaving no traceback and no log entry;
when that happens Send stops working with nothing in the terminal to say so. An
unexpected exit is therefore restarted on the same port, so the browser tab
reconnects on its own and the highlights on screen survive.

A clean exit stops the supervisor too, since that is the viewer's End session
button rather than a failure.
"""
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SERVER = HERE / "server.py"


def free_port(start: int) -> int:
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("no free port")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    out = Path(a.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    log = (out.parent / "server.log").open("a")

    port = free_port(a.port)
    print(f"http://127.0.0.1:{port}", flush=True)

    quick_failures = 0
    while True:
        started = time.time()
        proc = subprocess.Popen(
            [sys.executable, str(SERVER), "--pdf", a.pdf, "--out", str(out),
             "--port", str(port)],
            stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        rc = proc.wait()
        lived = time.time() - started
        if rc == 0:
            print("session ended", flush=True)
            return 0
        # A server that cannot even start is a config problem; do not spin on it.
        quick_failures = quick_failures + 1 if lived < 5 else 0
        if quick_failures >= 5:
            print(f"server keeps failing to start (rc {rc}); see "
                  f"{out.parent / 'server.log'}", flush=True)
            return 1
        with (out.parent / "events.log").open("a") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  server exited "
                     f"rc={rc} after {lived:.0f}s; restarting on {port}\n")
        print(f"server exited (rc {rc}) after {lived:.0f}s; restarting on {port}",
              flush=True)
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
