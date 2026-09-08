# overleaf-review

A Claude Code skill for marking up a compiled paper by hand and having the notes
applied to the LaTeX source.

You highlight passages in the rendered PDF and type a note on each. Claude finds
each quoted passage in the `.tex` source, makes the edit, recompiles, and the
page reloads on the new PDF. Then it waits for your next pass.

![The review viewer: a rendered page with three highlighted passages, and the sidebar holding a note against each one](docs/review.png)

<sub>Example pass over “High Speed Robotic Table Tennis Swinging Using Lightweight Hardware with Model Predictive Control” (ICRA 2025).</sub>

> **Requires the [Overleaf Connect][oc] VS Code extension.** It is what puts your
> Overleaf project on local disk and compiles it locally. See
> [Requirements](#requirements) — this skill does not talk to Overleaf itself and
> will not work against a project that only lives in the web editor.

[oc]: https://marketplace.visualstudio.com/items?itemName=tansuasici.overleaf-connect

## Why not just comment in a PDF reader

A PDF annotation lives in the build output. Every recompile throws it away, and
nothing connects a sticky note to the line of `.tex` that produced the text
underneath it. This keeps the comment attached to the quoted text rather than to
a page coordinate, so the match survives a reflow, and it hands the batch to
something that can make the edit.

## Requirements

**[Overleaf Connect][oc]** (`tansuasici.overleaf-connect`), a VS Code extension
that clones an Overleaf project over Git, keeps it synced, and compiles LaTeX
locally. This skill is built on top of what it provides and does not replace any
part of it:

- Overleaf Connect clones and auto-syncs the project, so the `.tex` files exist
  on disk where Claude can edit them, and edits push back to Overleaf.
- It compiles locally, so there is a `root.pdf` on disk to serve and rebuild.
  This skill reads that file; it never asks Overleaf to compile.

Install it from the VS Code marketplace, run **Configure Overleaf Connection**
and **Clone Overleaf Project**, and confirm you can compile — you want a real
PDF next to your sources before going any further. Source:
[tansuasici/OverleafConnect](https://github.com/tansuasici/OverleafConnect).

If your paper is already a local Git repo with a working `pdflatex` build and no
Overleaf involved, the skill will run on it unchanged. Overleaf Connect is what
gets an *Overleaf-hosted* paper into that state, which is what this was written
for.

Also needed:

- **Claude Code**, since the skill is a set of instructions it follows.
- **Python 3.9+**, standard library only. No packages to install.
- **A LaTeX toolchain** — whatever Overleaf Connect is configured to call.
- **A browser with network access on first load.** The viewer pulls PDF.js from
  a CDN. Nothing else leaves the machine; the server binds `127.0.0.1` and the
  PDF and your comments never go anywhere.

## Install

```bash
git clone https://github.com/davidhnguyen2000/overleaf-review.git \
  ~/.claude/skills/pdf-review
```

Claude Code takes the skill name from the directory, so keep it `pdf-review`
unless you also change the `name:` field in `SKILL.md`.

Then add the Stop hook to `~/.claude/settings.json`. It is what flips the viewer
out of its working state when a turn actually ends — without it the spinner
never stops:

```json
{
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/skills/pdf-review/turn_end.py 2>/dev/null || true",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

## Use

In a Claude Code session, in the directory Overleaf Connect cloned into:

```
/pdf-review
```

Claude compiles if the PDF is stale, starts the server, prints a `127.0.0.1`
URL, and opens it. Highlight a passage, type the note, press **Send to Claude**.

The **Overall note** field takes anything not tied to one passage — a length
target, a section to cut, a question to answer — and governs the pass as a whole
rather than being applied as one more comment.

While Claude works, the sidebar names the step it is on and offers a **Stop**
that halts it at the next checkpoint, leaving the edits it already made in
place:

![The sidebar mid-pass, reading "Claude is working" with the current step and a Stop button](docs/working.png)

When the rebuild lands, the page reloads on the new PDF and Claude arms itself
for another pass. **End session** shuts the server down.

## How the loop works

The part worth knowing, because it is what makes the round trip reliable:

- A Send appends to `.pdf-review/pending.md` and bumps a counter in
  `queue.json`. It never overwrites an earlier send.
- `wait.py` blocks until that counter runs ahead of the claim mark, then claims
  the batch and exits. That exit is what wakes Claude — it is the entire
  notification mechanism.
- Because the signal is a counter rather than a file's existence, a Send that
  lands while Claude is mid-edit is queued, not lost. The next armed waiter sees
  the gap and fires immediately.
- `wait.py` heartbeats while it waits, and the viewer turns its status line red
  when nothing is listening, so a missed wake-up is visible rather than silent.

Everything lives under `.pdf-review/` in the working directory: `pending.md`
(unclaimed sends), `inbox/` (claimed ones), the counters, the heartbeat, and
`events.log`, which records every send and every claim.

Add `.pdf-review/` to your paper's `.gitignore` — Overleaf Connect will
otherwise sync it up to Overleaf.

## Files

| file | what it is |
| --- | --- |
| `SKILL.md` | the instructions Claude follows |
| `server.py` | localhost HTTP server: serves the PDF and the viewer, collects sends |
| `viewer.html` | the review GUI — PDF.js render, text-layer selection, sidebar |
| `wait.py` | blocks until a send lands, then claims it; the wake-up mechanism |
| `notify.py` | writes progress into the viewer's status line |
| `turn_end.py` | Stop hook; the only thing that marks the session idle |

## Self-test

`?probe=1` on the viewer URL checks that the invisible text spans sit on top of
the rendered ink, and POSTs the result to `.pdf-review/probe.json`. `ratio: 1`
means selection matches what you see. A low ratio means the text layer is
misaligned — usually a missing `--scale-factor` CSS variable, which PDF.js 3.x
requires.

## Licence

MIT.
