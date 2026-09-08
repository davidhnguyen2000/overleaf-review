---
name: pdf-review
description: Open a rendered PDF in a local browser GUI where the user highlights text and attaches comments, then apply those comments as edits to the LaTeX source and rebuild. Use when the user says "review the PDF", "let me mark up the paper", "/pdf-review", or wants to annotate a build and have the notes applied to the source.
---

# PDF review

Round trip: user highlights passages in the rendered PDF and writes a note on
each; the notes arrive here as quoted text plus a page number; you locate each
quote in the `.tex` source, edit, and rebuild; the viewer reloads on the new PDF.

**The one rule that keeps the loop alive: a waiter must be armed whenever you
are not editing.** A Send only wakes you because a background `wait.py` exits,
and nothing else will. Arm it before you stop talking, every single time — after
the first round, after each rebuild, after a stop, after a question. If you end
a turn with no waiter armed, the user's next Send lands in a file nobody is
watching and the session looks dead to them.

## Step 1: Start the viewer

Default to `root.pdf` in the working directory unless the user names another.
Rebuild first if the PDF is older than any `.tex` file, so the user marks up
current output.

Run the server with Bash `run_in_background: true`:

```bash
python3 ~/.claude/skills/pdf-review/server.py \
  --pdf root.pdf --out .pdf-review/pending.md
```

The first line of output is the URL. Read it from the background output, open it
(`open <url>`), and tell the user the URL in case the browser does not focus.

A second line about unclaimed sends means a previous Send was never applied. It
is still in `pending.md` and the first waiter will hand it to you — say so
rather than starting a fresh pass on top of it.

Then say: highlight a passage, type the note, press **Send to Claude**. Arm the
waiter (Step 2) and stop talking.

## Step 2: Arm the waiter

```bash
python3 ~/.claude/skills/pdf-review/wait.py --dir .pdf-review
```

Bash `run_in_background: true`, always. It exits the moment a Send lands, and
that exit is the notification. It prints the claimed file's path and its
contents, so the comments arrive with the wake-up.

The signal is a counter in `.pdf-review/queue.json`, not a file's existence, so
a Send that arrives while you are mid-edit is not lost: it bumps `seq`, and the
next arm sees `seq` ahead of `claimed` and fires immediately. That is why
re-arming is enough and polling never is.

Do not hand-roll an `until [ -f ... ]` loop, and do not use Monitor. Arming
twice is harmless — the second exits with "already armed" — but do not do it on
purpose. While waiting, do nothing else unless the user asks.

The viewer shows the user a live **Claude is listening** line fed by the
waiter's heartbeat, and turns it red when nothing is armed. If they report the
red line, arm a waiter; do not tell them their comments were lost, because
`pending.md` still holds them.

Never restart the server while the user is annotating. A fresh page load clears
the sidebar, which looks to them like the work vanished. `.pdf-review/events.log`
records every Send, every claim, and every Send that arrived with no waiter
armed, so it is the place to check when the user asks whether their comments
landed.

## Step 3: Read the whole paper, then match

An `### Overall` section is the user's instruction for the pass as a whole — a
section to cut, a length target, a question to answer. Read it first and let it
govern the individual edits; it is not one more comment to apply in isolation.
It can arrive with no highlights at all.

A claimed file can hold more than one `## Send` block, when the user sent again
while you were working. Apply them in order and treat a later Overall as
governing.

`cat` every `.tex` file the document includes before matching anything. Papers
are small; holding all of the source at once makes matching reliable and is
cheaper than a search per quote.

Match each quote semantically, not literally. Extracted PDF text differs from
the source in predictable ways:

- math renders stripped of delimiters — `$1.14\times$` extracts as `1.14×`
- ligatures, en/em dashes, and quotes come out as Unicode
- line-broken words may carry a hyphen that is not in the source
- `\cite`/`\ref` render as the formatted number, not the key

The page number narrows a quote that has a near-twin elsewhere — in a paper
whose abstract, intro, and conclusion restate the same results, that is common.
If a quote still maps to two places, ask which one rather than guessing.

## Step 4: Edit

Keep the viewer honest about what you are doing. The user has no other view into
this, and silence looks identical to a crash.

```bash
python3 ~/.claude/skills/pdf-review/notify.py working "applying comment 2 of 4"
```

Call it when you start, on each comment, and before the rebuild. A message
naming the current step is the point; "working" on its own tells the user
nothing.

**You never mark the session idle.** `notify.py` takes `working` and `done`
only. `done` means the edits are finished, and the viewer keeps its spinner and
reads "wrapping up" until the turn actually ends — the Stop hook
(`turn_end.py`, wired into `~/.claude/settings.json`) writes `idle`, because
only the harness knows when you have stopped writing. Announcing idle yourself
is what used to make the GUI go quiet while the reply was still arriving.

**Check for a stop before every edit and after every rebuild:**

```bash
[ -f .pdf-review/stop ] && echo STOP
```

If the flag is there, stop at that point. Do not start the next edit. Then:
remove the flag, run `notify.py done "stopped after N of M"`, arm a waiter, and
report exactly which comments were applied, which were not, and whether the PDF
on disk matches the source. Leave the applied edits in place — the user asked
you to stop, not to revert. A half-applied pass they can see beats a silent
rollback.

Stopping is cooperative and cannot interrupt a command already running, so a
`pdflatex` pass in flight will finish. If the user needs a hard stop, that is
Ctrl+C in the terminal; say so if they ask why the button was not instant.

One comment, one edit. Obey the repo's own writing rules (`CLAUDE.md`) — voice,
page budget, and the rule that an addition is paid for by a cut. If a comment
asks for something that breaks a stated rule, do it and say so plainly; do not
silently refuse.

If a comment is a question rather than an instruction, answer it in the reply
and only edit if the answer implies a change.

## Step 5: Rebuild, re-arm, report

Rebuild per the repo's convention — usually `pdflatex -interaction=nonstopmode
root.tex` twice, from the repo root. Check `root.log` for `Overfull` and check
the page count. The viewer polls the PDF mtime and reloads within ~2s.

Then, in this order:

1. `python3 ~/.claude/skills/pdf-review/notify.py done "<one-line summary>"`.
2. **Arm the next waiter** (Step 2), in the background. Do this before writing
   your reply, not after — it is the step that gets forgotten, and forgetting it
   is what breaks the loop.
3. Show `git diff --stat`, and the page count before and after.
4. List anything you could not map or chose not to do, and why.

There is nothing to delete: the waiter already moved the claimed comments into
`.pdf-review/inbox/`, so `pending.md` only ever holds work you have not seen.

Leave the server running for another pass. The user ends it themselves with the
**End session** button in the viewer, which shuts the server down cleanly and
prints `session ended`. If they ask you to stop it instead, use TaskStop — and
kill the waiter too, or it will hold a stale heartbeat.

## Notes

- Localhost only. It reads the PDF and writes files under `.pdf-review/`;
  nothing leaves the machine.
- `.pdf-review/` holds `pending.md` (unclaimed sends), `inbox/` (claimed ones),
  `queue.json` (the send and claim counters), `watcher.json` (the waiter's
  heartbeat), `status.json`, and `events.log`.
- Highlights are cleared on reload, since the old page coordinates do not
  survive a reflow.
- Comments with an empty note are dropped at send.

## Self-test

`?probe=1` on the viewer URL checks that the invisible text spans sit on top of
rendered ink, and POSTs the result to `.pdf-review/probe.json`:

```bash
open "<url>/?probe=1"   # or run headless Chrome against it
```

`ratio: 1` means the text layer is aligned. A low ratio means selection will not
match what the user highlights — the usual cause is a missing `--scale-factor`
CSS variable on the page wrapper, which PDF.js 3.x requires.

To check the wake-up path itself without the browser: arm `wait.py`, then
`curl -s -X POST <url>/comments -H 'Content-Type: application/json'
-d '{"comments":[{"page":1,"quote":"x","comment":"y"}],"general":""}'`. The
waiter should exit within about two seconds, and `/state` should report
`"listening": true` while it is armed.
