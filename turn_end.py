#!/usr/bin/env python3
"""Stop hook: flip the review viewer to idle when the turn actually ends.

Claude cannot tell the viewer it has finished, because the call announcing it
is itself part of an unfinished response — that is why the GUI used to go quiet
while text was still arriving. The harness fires Stop when the turn is over,
which is the only honest end-of-work signal, so that is what writes `idle`.

Reads the hook payload on stdin, looks for .pdf-review/status.json under the
session's cwd, and rewrites it only when the state is one Claude set. A
`queued` state is left alone: comments waiting to be picked up are not done.
"""
import json
import os
import sys
import time
from pathlib import Path

try:
    payload = json.loads(sys.stdin.read() or "{}")
except ValueError:
    payload = {}

cwd = Path(payload.get("cwd") or os.getcwd())
status = cwd / ".pdf-review" / "status.json"
try:
    st = json.loads(status.read_text())
except (OSError, ValueError):
    sys.exit(0)

state = st.get("state")
if state not in ("working", "done"):
    sys.exit(0)

message = st.get("message", "") if state == "done" else (
    "Claude stopped before saying it was finished — check the terminal")
status.write_text(json.dumps(
    {"state": "idle", "message": message, "ts": time.time()}))
