#!/usr/bin/env python3
"""Serve a PDF with a highlight-and-comment viewer, and collect the comments.

    python3 server.py --pdf root.pdf --out .pdf-review/pending.md [--port 8765]

GET  /         the viewer
GET  /pdf      the PDF bytes (no-cache, so a rebuild is picked up)
GET  /mtime    {"mtime": <float>} for the viewer's reload poll
GET  /state    one poll: pdf mtime, agent status, stop flag, queue counters,
               and whether a waiter is listening
POST /comments {"comments":[{"page":int,"quote":str,"comment":str}],"general":str}
               appends a batch to --out, bumps the send counter, returns {"ok":true}

A Send is never lost and never silently overwritten. Each one increments
queue.json's "seq" and appends to the pending file; wait.py blocks until "seq"
runs ahead of "claimed" and claims the file. A Send that lands while Claude is
busy therefore still fires the next time a waiter is armed.
"""
from __future__ import annotations

import argparse
import http.server
import json
import socket
import threading
import urllib.parse
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STALE = 8.0  # a watcher heartbeat older than this means nobody is listening
LOCK = threading.Lock()


def read_queue(d: Path) -> dict:
    try:
        q = json.loads((d / "queue.json").read_text())
        return {"seq": int(q.get("seq", 0)), "claimed": int(q.get("claimed", 0))}
    except (OSError, ValueError, TypeError):
        return {"seq": 0, "claimed": 0}


def write_queue(d: Path, q: dict) -> None:
    (d / "queue.json").write_text(json.dumps(q))


def listening(d: Path) -> bool:
    """True if a wait.py heartbeat is fresh, so a Send will actually wake Claude."""
    try:
        w = json.loads((d / "watcher.json").read_text())
        return (time.time() - float(w.get("ts", 0))) < STALE
    except (OSError, ValueError, TypeError):
        return False


def log(d: Path, line: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    with (d / "events.log").open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {line}\n")


def build_handler(pdf: Path, out: Path):
    d = out.parent

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):  # keep the terminal quiet
            pass

        def _send(self, code, body: bytes, ctype: str):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            route = urllib.parse.urlsplit(self.path).path
            if route in ("/", "/index.html"):
                self._send(200, (HERE / "viewer.html").read_bytes(), "text/html")
            elif route == "/pdf":
                self._send(200, pdf.read_bytes(), "application/pdf")
            elif route == "/mtime":
                m = pdf.stat().st_mtime if pdf.exists() else 0
                self._send(200, json.dumps({"mtime": m}).encode(), "application/json")
            elif route == "/state":
                # everything the viewer needs for one poll
                q = read_queue(d)
                body = {"mtime": pdf.stat().st_mtime if pdf.exists() else 0,
                        "stop": (d / "stop").exists(),
                        "listening": listening(d),
                        "unclaimed": q["seq"] - q["claimed"],
                        "state": "idle", "message": "", "ts": 0}
                try:
                    body.update(json.loads((d / "status.json").read_text()))
                except (OSError, ValueError):
                    pass
                self._send(200, json.dumps(body).encode(), "application/json")
            else:
                self._send(404, b"not found", "text/plain")

        def do_POST(self):
            route = urllib.parse.urlsplit(self.path).path
            if route == "/probe":
                # viewer self-test (?probe=1) reports text-layer alignment here
                n = int(self.headers.get("Content-Length", 0))
                d.mkdir(parents=True, exist_ok=True)
                (d / "probe.json").write_bytes(self.rfile.read(n))
                self._send(200, b"{}", "application/json")
                return
            if route == "/stop":
                # cooperative: Claude checks for this between steps
                d.mkdir(parents=True, exist_ok=True)
                (d / "stop").write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
                log(d, "STOP requested")
                print("stop requested", flush=True)
                self._send(200, b"{}", "application/json")
                return
            if route == "/shutdown":
                # user ended the session from the viewer
                self._send(200, b"{}", "application/json")
                threading.Thread(target=self.server.shutdown, daemon=True).start()
                return
            if route != "/comments":
                self._send(404, b"not found", "text/plain")
                return
            n = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(n) or b"{}")
            items = payload.get("comments", [])
            general = str(payload.get("general", "")).strip()
            heard = listening(d)
            with LOCK:
                q = read_queue(d)
                q["seq"] += 1
                append_batch(items, general, pdf, out, q["seq"])
                write_queue(d, q)
                (d / "stop").unlink(missing_ok=True)
                # queued, not working: nothing is working until a waiter claims it
                (d / "status.json").write_text(json.dumps(
                    {"state": "queued", "message": "waiting to be picked up",
                     "ts": time.time()}))
            log(d, f"sent {len(items)} comment(s)"
                   f"{' + overall note' if general else ''} -> send {q['seq']}"
                   f"{'' if heard else '  [NO LISTENER ARMED]'}")
            print(f"received {len(items)} comment(s) as send {q['seq']}"
                  + ("" if heard else "  -- WARNING: no waiter armed, "
                                      "Claude will not be woken"), flush=True)
            self._send(200, json.dumps({"ok": True, "listening": heard,
                                        "seq": q["seq"]}).encode(),
                       "application/json")

    return Handler


def append_batch(comments, general: str, pdf: Path, out: Path, seq: int) -> None:
    """Append one Send to the pending file as the chat entry Claude will read."""
    lines = []
    if not out.exists():
        lines += [f"# PDF review — {pdf.name}", ""]
    lines += [f"## Send {seq} · {len(comments)} comment(s) · "
              f"{time.strftime('%Y-%m-%d %H:%M')}", ""]
    if general:
        lines += ["### Overall", "", general, ""]
    for i, c in enumerate(comments, 1):
        quote = " ".join(str(c.get("quote", "")).split())
        lines += [f"### {i} · p.{c.get('page', '?')}", "", f"> “{quote}”", ""]
        lines += [str(c.get("comment", "")).strip() or "_(no comment)_", ""]
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a") as fh:
        fh.write("\n".join(lines) + "\n")


def free_port(start: int) -> int:
    for p in range(start, start + 20):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise SystemExit("no free port")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--port", type=int, default=8765)
    a = ap.parse_args()

    pdf = Path(a.pdf).resolve()
    if not pdf.exists():
        raise SystemExit(f"no such pdf: {pdf}")
    out = Path(a.out).resolve()
    d = out.parent
    d.mkdir(parents=True, exist_ok=True)

    # A pending file left from a previous session is unapplied work, not litter.
    # Rather than archiving it out of sight, put the counter ahead of the claim
    # mark so the first waiter picks it up and Claude is told about it.
    q = read_queue(d)
    leftover = 0
    if out.exists() and q["seq"] <= q["claimed"]:
        q["seq"] = q["claimed"] + 1
        write_queue(d, q)
    if out.exists():
        leftover = q["seq"] - q["claimed"]
    # a watcher from a dead session must not make the viewer claim someone
    # is listening
    (d / "watcher.json").unlink(missing_ok=True)

    port = free_port(a.port)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), build_handler(pdf, out))
    print(f"http://127.0.0.1:{port}", flush=True)
    if leftover:
        print(f"note: {out.name} holds {leftover} unclaimed send(s) from before; "
              f"the first wait.py will pick them up", flush=True)
    log(d, f"server up on {port} for {pdf.name}")
    try:
        srv.serve_forever()
    finally:
        log(d, "session ended")
        print("session ended", flush=True)


if __name__ == "__main__":
    main()
