#!/usr/bin/env python3
"""Block until the reviewer sends a batch, then claim it.

    python3 wait.py [--dir .pdf-review] [--timeout SECONDS]

Run this with Bash run_in_background: the process exits the moment a Send
lands, and the exit is what wakes Claude.

The signal is a counter, not a file that happens to exist. server.py bumps
queue.json's "seq" on every Send; this blocks until "seq" runs ahead of
"claimed", then claims the batch by moving the pending file into inbox/ and
setting claimed = seq. So a Send that arrives while Claude is mid-edit is not
lost: the next arm sees the gap and returns immediately.

While it waits it refreshes watcher.json every couple of seconds. The server
reads that heartbeat to tell the viewer whether anyone is listening, which is
what turns a missed wake-up from silence into a visible red line in the GUI.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

BEAT = 2.0          # seconds between heartbeats
STALE = 8.0         # a heartbeat older than this means the waiter is gone


def read_queue(d: Path) -> dict:
    try:
        q = json.loads((d / "queue.json").read_text())
        return {"seq": int(q.get("seq", 0)), "claimed": int(q.get("claimed", 0))}
    except (OSError, ValueError, TypeError):
        return {"seq": 0, "claimed": 0}


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (OSError, TypeError):
        return False
    return True


def already_armed(d: Path) -> int | None:
    """pid of a live waiter on this directory, or None."""
    try:
        w = json.loads((d / "watcher.json").read_text())
    except (OSError, ValueError):
        return None
    pid = int(w.get("pid", 0))
    if pid == os.getpid():
        return None
    if time.time() - float(w.get("ts", 0)) > STALE:
        return None
    return pid if alive(pid) else None


def beat(d: Path, since: float) -> None:
    (d / "watcher.json").write_text(json.dumps(
        {"pid": os.getpid(), "ts": time.time(), "since": since}))


def claim(d: Path, out: Path, q: dict) -> Path | None:
    """Move the pending batches out of the way and mark them claimed."""
    inbox = d / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    dest = None
    try:
        dest = inbox / f"{time.strftime('%Y%m%d-%H%M%S')}-send{q['seq']}{out.suffix}"
        out.rename(dest)
    except OSError:
        # another waiter got there first, or the file never landed
        dest = None
    q["claimed"] = q["seq"]
    (d / "queue.json").write_text(json.dumps(q))
    (d / "status.json").write_text(json.dumps(
        {"state": "working", "message": "picked up — reading the source",
         "ts": time.time()}))
    with (d / "events.log").open("a") as fh:
        fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  claimed send "
                 f"{q['seq']} -> {dest.name if dest else '(no file)'}\n")
    return dest


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=".pdf-review")
    ap.add_argument("--out", default=None,
                    help="pending file (default <dir>/pending.md)")
    ap.add_argument("--timeout", type=float, default=0,
                    help="give up after N seconds; 0 waits forever")
    a = ap.parse_args()

    d = Path(a.dir).resolve()
    d.mkdir(parents=True, exist_ok=True)
    out = Path(a.out).resolve() if a.out else d / "pending.md"

    other = already_armed(d)
    if other:
        print(f"already armed by pid {other} — nothing to do, do not arm again")
        return 0

    since = time.time()
    beat(d, since)
    deadline = since + a.timeout if a.timeout else None
    try:
        while True:
            q = read_queue(d)
            if q["seq"] > q["claimed"]:
                dest = claim(d, out, q)
                print(f"NEW REVIEW BATCH (send {q['seq']})")
                if dest:
                    print(f"file: {dest}")
                    print()
                    print(dest.read_text()[:4000])
                else:
                    print("the send counter moved but no pending file was found; "
                          "check .pdf-review/events.log")
                return 0
            if deadline and time.time() > deadline:
                print(f"no send after {a.timeout:.0f}s — arm again to keep listening")
                return 0
            beat(d, since)
            time.sleep(BEAT)
    finally:
        try:
            w = json.loads((d / "watcher.json").read_text())
            if int(w.get("pid", 0)) == os.getpid():
                (d / "watcher.json").unlink(missing_ok=True)
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    raise SystemExit(main())
