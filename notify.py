#!/usr/bin/env python3
"""Report progress to the review viewer.

    python3 notify.py working "applying comment 2 of 4"
    python3 notify.py done    "4 edits, 9 pages"

Writes <dir>/status.json, which the server serves to the viewer. --dir defaults
to .pdf-review under the current directory.

`done` means "the edits are finished", not "the turn is over": the viewer keeps
the spinner up and reads "wrapping up" until the turn actually ends. Only the
Stop hook (turn_end.py) writes `idle`, because only the harness knows when
Claude has stopped talking. Calling `idle` by hand is what made the GUI go
quiet while Claude was still writing, so it is not offered here.
"""
import argparse
import json
import time
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("state", choices=["working", "done"])
ap.add_argument("message", nargs="?", default="")
ap.add_argument("--dir", default=".pdf-review")
a = ap.parse_args()

d = Path(a.dir)
d.mkdir(parents=True, exist_ok=True)
(d / "status.json").write_text(json.dumps(
    {"state": a.state, "message": a.message, "ts": time.time()}))
print(f"{a.state}: {a.message}")
