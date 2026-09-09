#!/usr/bin/env python3
"""Overleaf git-bridge front end for the pdf-review skill.

Everything here rides Overleaf's git bridge, which is the interface Overleaf
publishes for programmatic access. No cookies are required for the default
path; the optional compile mode is the one unsupported piece and it always
falls back to a local build rather than ending a review session.

  login    --token olp_… | --from-remote DIR    store the account git token
  secure   DIR                                  strip a token out of a remote
  session  --cookie … | --clear                 store overleaf_session2
  open     URL|NAME [--name N] [--dir D]        clone or pull; prints the dir
  build    [--mode local|overleaf] [--dir D]    build the PDF
  push     [-m MSG] [--dir D]                   commit, pull --rebase, push
  setup                                         check the install, say what is missing
  list                                          known projects
  vendor-bst [--dir D]                          copy IEEEtran.bst into the repo
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REGISTRY = HERE / "projects.json"
SESSION = HERE / "session.json"
ASKPASS = HERE / "askpass.sh"
KEYCHAIN_SERVICE = "overleaf-git-token"  # keyring service name, all platforms
DEFAULT_ROOT = Path.home() / "Documents" / "Overleaf"
PROJECT_ID = re.compile(r"[0-9a-f]{24}")

# Build artifacts and skill state must never be pushed into someone's Overleaf
# project. These go in .git/info/exclude, which is local and never committed.
LOCAL_EXCLUDES = [
    ".pdf-review/", "*.aux", "*.log", "*.out", "*.bbl", "*.blg", "*.fls",
    "*.fdb_latexmk", "*.synctex.gz", "*.toc", "*.lof", "*.lot",
    # top-level build output only; figures/*.pdf must stay tracked
    "/*.pdf",
]


# ---------------------------------------------------------------- credentials
#
# One token, stored in the best place the machine offers. In order: an
# environment variable (CI, containers, headless boxes), the macOS keychain,
# libsecret on Linux, then a 0600 file next to this script. The file is a real
# fallback, not a failure: plenty of Linux machines have no keyring daemon
# running, and a review session must not depend on one.
#
# Every lookup is time-limited. A credential helper that blocks on a GUI unlock
# prompt is indistinguishable from a hung clone, and that failure has already
# cost us once.

CRED_FILE = HERE / "credentials.json"
ENV_VAR = "OVERLEAF_GIT_TOKEN"
CRED_TIMEOUT = 10


def _run(cmd: list[str], stdin: str | None = None):
    try:
        return subprocess.run(cmd, input=stdin, capture_output=True, text=True,
                              timeout=CRED_TIMEOUT)
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return None


def _have(exe: str) -> bool:
    from shutil import which
    return which(exe) is not None


def _mac_get() -> str | None:
    r = _run(["security", "find-generic-password", "-a", os.environ.get("USER", ""),
              "-s", KEYCHAIN_SERVICE, "-w"])
    return (r.stdout.strip() or None) if r and r.returncode == 0 else None


def _mac_set(token: str) -> bool:
    # -A: readable by any process running as this user, without an approval
    # dialog. Without it git's askpass blocks on a GUI prompt and the clone
    # hangs with no output. Practical exposure matches a 0600 file, but the
    # token is encrypted at rest and stays out of git config and shell history,
    # which is what we are actually fixing.
    r = _run(["security", "add-generic-password", "-a", os.environ.get("USER", ""),
              "-s", KEYCHAIN_SERVICE, "-w", token, "-U", "-A"])
    return bool(r and r.returncode == 0)


def _secret_get() -> str | None:
    r = _run(["secret-tool", "lookup", "service", KEYCHAIN_SERVICE,
              "account", os.environ.get("USER", "")])
    return (r.stdout.strip() or None) if r and r.returncode == 0 else None


def _secret_set(token: str) -> bool:
    r = _run(["secret-tool", "store", "--label=Overleaf git token",
              "service", KEYCHAIN_SERVICE, "account", os.environ.get("USER", "")],
             stdin=token)
    return bool(r and r.returncode == 0)


def _file_get() -> str | None:
    try:
        return json.loads(CRED_FILE.read_text()).get("token") or None
    except (OSError, ValueError):
        return None


def _file_set(token: str) -> bool:
    try:
        CRED_FILE.write_text(json.dumps({"token": token}))
        CRED_FILE.chmod(0o600)
        return True
    except OSError:
        return False


BACKEND_VAR = "OVERLEAF_CRED_BACKEND"


def backends() -> list[tuple[str, object, object]]:
    """(name, getter, setter) for the stores this machine can actually use.

    $OVERLEAF_CRED_BACKEND pins one of keychain/libsecret/file, for a machine
    whose keyring is present but misbehaving.
    """
    pin = os.environ.get(BACKEND_VAR, "").strip().lower()
    out = []
    if sys.platform == "darwin" and _have("security"):
        out.append(("keychain", "macOS keychain", _mac_get, _mac_set))
    if _have("secret-tool"):
        out.append(("libsecret", "libsecret", _secret_get, _secret_set))
    out.append(("file", f"file {CRED_FILE.name} (0600)", _file_get, _file_set))
    if pin:
        out = [b for b in out if b[0] == pin] or out
    return [(label, g, st) for _key, label, g, st in out]


def cred_get() -> str | None:
    env = os.environ.get(ENV_VAR, "").strip()
    if env:
        return env
    for _, get, _set in backends():
        v = get()
        if v:
            return v
    return None


def cred_set(token: str) -> str:
    """Store the token, returning the name of the store that took it."""
    errors = []
    for name, _get, setter in backends():
        if setter(token):
            # Confirm it reads back. A keyring that accepts a write and then
            # cannot be read is worse than never having used it.
            if _get() == token or name.startswith("file"):
                return name
            errors.append(f"{name}: wrote but could not read back")
        else:
            errors.append(f"{name}: unavailable")
    raise SystemExit("could not store the token anywhere:\n  "
                     + "\n  ".join(errors))


def cred_where() -> str | None:
    """Which store currently holds a token, for the setup report."""
    if os.environ.get(ENV_VAR, "").strip():
        return f"${ENV_VAR}"
    for name, get, _s in backends():
        if get():
            return name
    return None


def write_askpass() -> Path:
    """The helper git calls for credentials, so the token never enters argv.

    It defers straight back to this script rather than embedding any one
    platform's lookup command, so every backend works the same way.
    """
    ASKPASS.write_text(
        "#!/bin/sh\n"
        f'exec {sys.executable} "{Path(__file__).resolve()}" _askpass "$1"\n')
    ASKPASS.chmod(0o700)
    return ASKPASS


def cmd_askpass(a) -> int:
    if "user" in (a.prompt or "").lower():
        print("git")
        return 0
    tok = cred_get()
    if not tok:
        return 1
    print(tok)
    return 0


def git_env() -> dict:
    env = dict(os.environ)
    env["GIT_ASKPASS"] = str(write_askpass())
    env["GIT_TERMINAL_PROMPT"] = "0"
    # SSH_ASKPASS_REQUIRE / DISPLAY are irrelevant for https, but an unset
    # DISPLAY stops some helpers from trying to open a GUI prompt at all.
    env.pop("SSH_ASKPASS", None)
    return env


def redact(s: str) -> str:
    return re.sub(r"olp_[A-Za-z0-9]+", "olp_REDACTED", s)


# ------------------------------------------------------------------- registry

def load_registry() -> dict:
    try:
        return json.loads(REGISTRY.read_text())
    except (OSError, ValueError):
        return {}


def save_registry(reg: dict) -> None:
    REGISTRY.write_text(json.dumps(reg, indent=2, sort_keys=True) + "\n")


def resolve(target: str) -> tuple[str, str | None]:
    """Turn a pasted URL, a bare id, or a remembered name into a project id."""
    t = target.strip()
    reg = load_registry()
    if t in reg:
        return reg[t]["id"], t
    if re.fullmatch(r"[0-9a-f]{24}", t):
        return t, None
    if "/read/" in t or "/rw/" in t or re.search(r"overleaf\.com/[a-z]{10,}/?$", t):
        raise SystemExit(
            "That looks like a share or read-only link. It carries a share token,\n"
            "not a project id, and the git bridge cannot use it. Open the project\n"
            "in Overleaf and copy the editor URL instead:\n"
            "  https://www.overleaf.com/project/<24-hex-id>")
    m = PROJECT_ID.search(t)
    if m:
        return m.group(0), None
    raise SystemExit(f"could not find a project id in: {t}")


# ----------------------------------------------------------------------- git

def git(args: list[str], cwd: Path | None = None, check: bool = True,
        capture: bool = True) -> subprocess.CompletedProcess:
    # credential.helper= (empty) clears the inherited helper chain. Without it
    # git consults credential-osxkeychain first and that can block forever on a
    # GUI approval prompt, so the clone hangs with no output and no error.
    # GIT_ASKPASS is only reached once the helper chain is empty.
    r = subprocess.run(["git", "-c", "credential.helper="] + args,
                       cwd=str(cwd) if cwd else None,
                       env=git_env(), text=True,
                       capture_output=capture)
    if check and r.returncode != 0:
        out = redact((r.stdout or "") + (r.stderr or ""))
        raise SystemExit(f"git {' '.join(args[:2])} failed:\n{out}")
    return r


def set_local_excludes(work: Path) -> None:
    ex = work / ".git" / "info" / "exclude"
    ex.parent.mkdir(parents=True, exist_ok=True)
    have = ex.read_text() if ex.exists() else ""
    missing = [p for p in LOCAL_EXCLUDES if p not in have.split()]
    if missing:
        with ex.open("a") as fh:
            fh.write("\n# pdf-review: build artifacts and skill state\n")
            fh.write("\n".join(missing) + "\n")


def bridge_url(pid: str) -> str:
    return f"https://git.overleaf.com/{pid}"


# ---------------------------------------------------------------- subcommands

def cmd_login(a) -> int:
    token = a.token
    if a.from_remote:
        url = git(["remote", "get-url", "origin"], Path(a.from_remote)).stdout
        m = re.search(r"olp_[A-Za-z0-9]+", url)
        if not m:
            raise SystemExit(f"no olp_ token in the origin URL of {a.from_remote}")
        token = m.group(0)
    if not token:
        raise SystemExit("give --token olp_… or --from-remote DIR")
    if not token.startswith("olp_"):
        print("warning: tokens normally start with olp_", file=sys.stderr)
    where = cred_set(token)
    write_askpass()
    print(f"token stored in: {where}")
    print("it is account-wide, so it covers every project you own")
    if where.startswith("file"):
        print(f"\nNo system keyring was usable here, so the token is in "
              f"{CRED_FILE}, readable only by you. On Linux, installing "
              f"libsecret (`secret-tool`) and re-running login moves it into "
              f"the keyring. For a headless or shared machine, prefer the "
              f"{ENV_VAR} environment variable, which takes precedence over "
              f"every store and leaves nothing on disk.")
    return 0


def cmd_secure(a) -> int:
    """Take a token out of an existing repo's remote and into the keychain."""
    work = Path(a.dir).expanduser().resolve()
    url = git(["remote", "get-url", "origin"], work).stdout.strip()
    m = re.search(r"olp_[A-Za-z0-9]+", url)
    pid_m = PROJECT_ID.search(url)
    if not pid_m:
        raise SystemExit(f"origin does not look like an Overleaf remote: {redact(url)}")
    if m and not cred_get():
        print(f"token moved into: {cred_set(m.group(0))}")
    git(["remote", "set-url", "origin", bridge_url(pid_m.group(0))], work)
    set_local_excludes(work)
    write_askpass()
    print(f"origin is now {bridge_url(pid_m.group(0))} (no token on disk)")
    if m:
        print("note: the old URL may still be in this repo's reflog and in your "
              "shell history; rotate the token in Overleaf if that matters to you")
    return 0


def cmd_session(a) -> int:
    if a.clear:
        SESSION.unlink(missing_ok=True)
        print("session cookie cleared; builds will use latexmk")
        return 0
    if not a.cookie:
        raise SystemExit("give --cookie <overleaf_session2 value> or --clear")
    c = a.cookie.strip()
    if c.startswith("overleaf_session2="):
        c = c.split("=", 1)[1]
    SESSION.write_text(json.dumps({"overleaf_session2": c}))
    SESSION.chmod(0o600)
    print("session cookie stored. Overleaf compile mode is available via "
          "`build --mode overleaf`; it falls back to latexmk whenever the "
          "session is rejected.")
    return 0


def cmd_open(a) -> int:
    pid, known = resolve(a.target)
    reg = load_registry()
    name = a.name or known
    if not name:
        for k, v in reg.items():
            if v["id"] == pid:
                name = k
                break
    name = name or f"project-{pid[:8]}"

    # A remembered project reopens where it already lives. Falling through to
    # the default root would clone a second copy beside the first.
    if a.dir:
        work = Path(a.dir).expanduser().resolve()
    elif name in reg and reg[name].get("dir"):
        work = Path(reg[name]["dir"])
    else:
        work = DEFAULT_ROOT / name
    if not cred_get():
        raise SystemExit(
            "No Overleaf token stored yet. Create one in Overleaf under\n"
            "Account Settings -> Git integration, then run:\n"
            "  overleaf.py login --token olp_…\n"
            "Run `overleaf.py setup` to check the rest of the install.")
    write_askpass()

    if (work / ".git").exists():
        git(["remote", "set-url", "origin", bridge_url(pid)], work)
        r = git(["pull", "--rebase"], work, check=False)
        if r.returncode != 0:
            print(redact((r.stdout or "") + (r.stderr or "")), file=sys.stderr)
            print("pull failed; the working tree may have local changes. "
                  "Resolve them, then re-run.", file=sys.stderr)
            return 1
        action = "updated"
    else:
        work.parent.mkdir(parents=True, exist_ok=True)
        git(["clone", bridge_url(pid), str(work)])
        action = "cloned"

    set_local_excludes(work)
    reg[name] = {"id": pid, "dir": str(work)}
    save_registry(reg)
    head = git(["log", "--oneline", "-1"], work).stdout.strip()
    print(f"{action}: {name}")
    print(f"dir: {work}")
    print(f"head: {head}")
    return 0


# -------------------------------------------------------------------- builds

def find_root_tex(work: Path) -> Path:
    for cand in ("root.tex", "main.tex"):
        if (work / cand).exists():
            return work / cand
    for p in sorted(work.glob("*.tex")):
        if "\\documentclass" in p.read_text(errors="ignore"):
            return p
    raise SystemExit(f"no root .tex found in {work}")


def page_count(pdf: Path) -> int | None:
    try:
        r = subprocess.run(["pdfinfo", str(pdf)], capture_output=True, text=True)
        for line in r.stdout.splitlines():
            if line.lower().startswith("pages:"):
                return int(line.split(":")[1])
    except (FileNotFoundError, ValueError):
        pass
    try:
        data = pdf.read_bytes()
        n = len(re.findall(rb"/Type\s*/Page[^s]", data))
        return n or None
    except OSError:
        return None


def build_local(work: Path) -> Path:
    """latexmk, not pdflatex twice: a fresh clone has no .bbl, and pdflatex
    alone leaves every citation undefined."""
    root = find_root_tex(work)
    subprocess.run(["latexmk", "-pdf", "-interaction=nonstopmode", root.name],
                   cwd=str(work), capture_output=True, text=True)
    pdf = root.with_suffix(".pdf")
    if not pdf.exists():
        raise SystemExit(f"latexmk produced no PDF; see {root.with_suffix('.log')}")
    return pdf


def build_overleaf(work: Path, pid: str) -> Path | None:
    """Compile on Overleaf. Returns None on any failure, so the caller falls
    back to a local build rather than ending the review session."""
    try:
        cookie = json.loads(SESSION.read_text())["overleaf_session2"]
    except (OSError, ValueError, KeyError):
        print("no session cookie stored; run `overleaf.py session --cookie …`",
              file=sys.stderr)
        return None

    hdrs = {"Cookie": f"overleaf_session2={cookie}",
            "User-Agent": "Mozilla/5.0 pdf-review"}

    def get(url: str, headers: dict, data: bytes | None = None):
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method="POST" if data else "GET")
        return urllib.request.urlopen(req, timeout=120)

    base = "https://www.overleaf.com"
    try:
        with get(f"{base}/project/{pid}", hdrs) as r:
            html = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        print(f"Overleaf returned {e.code} for the project page; the session "
              f"cookie is probably expired. Falling back to latexmk.",
              file=sys.stderr)
        return None
    except OSError as e:
        print(f"could not reach Overleaf ({e}); falling back to latexmk",
              file=sys.stderr)
        return None

    m = (re.search(r'name="ol-csrfToken"\s+content="([^"]+)"', html)
         or re.search(r'content="([^"]+)"\s+name="ol-csrfToken"', html)
         or re.search(r'"csrfToken"\s*:\s*"([^"]+)"', html))
    if not m:
        print("could not find a CSRF token on the project page; the page shape "
              "changed or the cookie is not logged in. Falling back to latexmk.",
              file=sys.stderr)
        return None
    csrf = m.group(1)

    body = json.dumps({"check": "silent", "draft": False,
                       "incrementalCompilesEnabled": True,
                       "stopOnFirstError": False}).encode()
    ch = dict(hdrs, **{"Content-Type": "application/json", "x-csrf-token": csrf,
                       "Accept": "application/json", "Referer": f"{base}/project/{pid}"})
    try:
        with get(f"{base}/project/{pid}/compile", ch, body) as r:
            resp = json.loads(r.read().decode("utf-8", "replace"))
    except (urllib.error.HTTPError, OSError, ValueError) as e:
        print(f"compile request failed ({e}); falling back to latexmk",
              file=sys.stderr)
        return None

    # Read the PDF's location out of the response rather than assuming a path,
    # so a change on their side surfaces as a clear error instead of a 404.
    out = None
    for f in resp.get("outputFiles", []):
        if str(f.get("path", "")).endswith("output.pdf"):
            out = f.get("url")
            break
    if not out:
        dbg = work / ".pdf-review" / "overleaf-compile.json"
        dbg.parent.mkdir(parents=True, exist_ok=True)
        dbg.write_text(json.dumps(resp, indent=2))
        print(f"no output.pdf in the compile response (status "
              f"{resp.get('status')!r}); raw response saved to {dbg}. "
              f"Falling back to latexmk.", file=sys.stderr)
        return None

    try:
        with get(out if out.startswith("http") else base + out, hdrs) as r:
            data = r.read()
    except (urllib.error.HTTPError, OSError) as e:
        print(f"could not download the compiled PDF ({e}); falling back",
              file=sys.stderr)
        return None

    pdf = find_root_tex(work).with_suffix(".pdf")
    pdf.write_bytes(data)
    return pdf


def cmd_build(a) -> int:
    work = Path(a.dir).expanduser().resolve() if a.dir else Path.cwd()
    root = find_root_tex(work)
    mode = a.mode
    pdf = None

    if mode == "overleaf":
        url = git(["remote", "get-url", "origin"], work, check=False).stdout
        m = PROJECT_ID.search(url or "")
        if not m:
            print("not an Overleaf checkout; building locally", file=sys.stderr)
        else:
            if a.push_first:
                cmd_push(argparse.Namespace(
                    dir=str(work), force_assets=False,
                    message=a.message or "pdf-review: sync before compile"))
            else:
                print("note: Overleaf compiles what is in the project, not your "
                      "working tree. Pass --push-first to sync your edits.",
                      file=sys.stderr)
            pdf = build_overleaf(work, m.group(0))
        if pdf is None:
            mode = "local (fell back)"

    if pdf is None:
        pdf = build_local(work)

    log = root.with_suffix(".log")
    undef = 0
    if log.exists():
        undef = len(re.findall(r"Citation .* undefined",
                               log.read_text(errors="ignore")))
        overfull = len(re.findall(r"Overfull", log.read_text(errors="ignore")))
    else:
        overfull = 0
    print(f"pdf: {pdf}")
    print(f"mode: {mode}")
    print(f"pages: {page_count(pdf)}")
    print(f"undefined citations: {undef}")
    print(f"overfull boxes: {overfull}")
    if undef:
        print("warning: citations did not resolve. If IEEEtran.bst is missing "
              "from the repo, run `overleaf.py vendor-bst`.", file=sys.stderr)
    return 0


GRAPHICS = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
ASSET_EXTS = ["", ".pdf", ".png", ".jpg", ".jpeg", ".eps"]


def dropped_assets(work: Path) -> list[str]:
    """Figures that exist on disk but git will silently skip.

    Overleaf Connect writes a .gitignore with a bare `*.pdf` rule. A project
    without a matching `!figures/*.pdf` negation therefore drops every PDF
    figure from `git add -A` — the local build keeps working because the file
    is still on disk, and Overleaf breaks with a missing figure. Catch it at
    push time, which is the last moment it is still cheap.
    """
    refs = set()
    for tex in work.rglob("*.tex"):
        try:
            refs.update(GRAPHICS.findall(tex.read_text(errors="ignore")))
        except OSError:
            continue
    bad = []
    for ref in sorted(refs):
        for ext in ASSET_EXTS:
            f = work / (ref + ext)
            if not f.is_file():
                continue
            rel = str(f.relative_to(work))
            tracked = git(["ls-files", "--error-unmatch", rel], work,
                          check=False).returncode == 0
            ignored = git(["check-ignore", "-q", rel], work,
                          check=False).returncode == 0
            if ignored and not tracked:
                bad.append(rel)
            break
    return bad


def cmd_push(a) -> int:
    work = Path(a.dir).expanduser().resolve() if a.dir else Path.cwd()
    set_local_excludes(work)

    bad = dropped_assets(work)
    if bad and not a.force_assets:
        print("These figures are referenced by the document, exist on disk, and "
              "are ignored by git, so pushing would leave Overleaf with a "
              "missing figure while your local build keeps working:\n",
              file=sys.stderr)
        for f in bad:
            print(f"  {f}", file=sys.stderr)
        print("\nFix the .gitignore (a `!figures/*.pdf` line after the `*.pdf` "
              "rule), or re-run with --force-assets to add them anyway. "
              "Nothing was pushed.", file=sys.stderr)
        return 1
    if bad:
        git(["add", "-f"] + bad, work)
        print(f"force-added {len(bad)} ignored figure(s)")

    git(["add", "-A"], work)
    staged = git(["diff", "--cached", "--name-only"], work).stdout.strip()
    if staged:
        git(["commit", "-m", a.message or "pdf-review: apply comments"], work)
    else:
        print("nothing to commit")
    r = git(["pull", "--rebase"], work, check=False)
    if r.returncode != 0:
        git(["rebase", "--abort"], work, check=False)
        print(redact((r.stdout or "") + (r.stderr or "")), file=sys.stderr)
        print("\nThe rebase onto Overleaf did not complete, so nothing was "
              "pushed. Any rebase in progress was aborted and your commit is "
              "intact on the local branch. The usual cause is a co-author "
              "editing the project in the web editor while you were editing "
              "here; the git output above says which. Resolve by hand:\n"
              f"  cd {work} && git pull --rebase", file=sys.stderr)
        return 1
    p = git(["push"], work, check=False)
    if p.returncode != 0:
        print(redact((p.stdout or "") + (p.stderr or "")), file=sys.stderr)
        return 1
    print(f"pushed: {git(['log', '--oneline', '-1'], work).stdout.strip()}")
    return 0


def cmd_list(a) -> int:
    reg = load_registry()
    if not reg:
        print("no projects yet. Open one:  overleaf.py open <overleaf-url>")
        return 0
    for name, v in sorted(reg.items()):
        exists = "" if Path(v["dir"]).exists() else "   (directory missing)"
        print(f"{name:24} {v['id']}  {v['dir']}{exists}")
    return 0


def cmd_vendor_bst(a) -> int:
    """IEEEtran.bst is the one build input that is not in the repo, so it is the
    one place your render and Overleaf's can differ."""
    work = Path(a.dir).expanduser().resolve() if a.dir else Path.cwd()
    if (work / "IEEEtran.bst").exists():
        print("IEEEtran.bst is already in the repo")
        return 0
    r = subprocess.run(["kpsewhich", "IEEEtran.bst"], capture_output=True, text=True)
    src = r.stdout.strip()
    if not src or not Path(src).exists():
        raise SystemExit("could not locate IEEEtran.bst in your TeX installation")
    (work / "IEEEtran.bst").write_bytes(Path(src).read_bytes())
    print(f"copied {src} -> {work / 'IEEEtran.bst'}")
    print("commit and push it so Overleaf uses the same bibliography style")
    return 0



def cmd_setup(a) -> int:
    """What is configured, what is missing, and the next command to run.

    Written to be run by a person or read aloud by Claude on first use, so
    every failure line carries its own fix rather than a diagnosis.
    """
    from shutil import which
    ok = "  ok  "
    bad = " MISS "
    todo = []

    print("Overleaf review — setup check\n")

    v = sys.version_info
    good = v >= (3, 9)
    print(f"[{ok if good else bad}] Python {v.major}.{v.minor}")
    if not good:
        todo.append("Python 3.9 or newer is required.")

    for exe, why in (("git", "cloning from Overleaf"),
                     ("latexmk", "building the PDF")):
        have = which(exe)
        print(f"[{ok if have else bad}] {exe}"
              + (f"  ({have})" if have else f"  — needed for {why}"))
        if not have:
            todo.append(
                f"Install {exe}." + ("" if exe == "git" else
                " It comes with TeX Live and MacTeX. latexmk rather than bare"
                " pdflatex matters: a fresh clone has no .bbl, and two pdflatex"
                " passes exit zero while leaving every citation undefined."))

    stores = [n for n, _g, _s in backends()]
    holder = cred_where()
    print(f"[{ok if holder else bad}] Overleaf token"
          + (f"  (in {holder})" if holder else "  — not stored yet"))
    print(f"         credential stores available here: {', '.join(stores)}")
    if not holder:
        todo.append(
            "Create a token in Overleaf under Account Settings -> Git "
            "integration, then run:\n"
            "    python3 " + str(Path(__file__).resolve()) + " login --token olp_…")

    settings = Path.home() / ".claude" / "settings.json"
    hooked = False
    try:
        hooked = "turn_end.py" in settings.read_text()
    except OSError:
        pass
    print(f"[{ok if hooked else bad}] Stop hook in {settings}")
    if not hooked:
        todo.append(
            "Add the Stop hook to " + str(settings) + ", or the viewer's "
            "spinner never stops:\n"
            '    {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": '
            '"python3 ' + str(HERE / "turn_end.py") + ' 2>/dev/null || true", '
            '"timeout": 5}]}]}}')

    reg = load_registry()
    print(f"[{ok if reg else '  --  '}] projects opened: "
          + (", ".join(sorted(reg)) if reg else "none yet"))

    print()
    if todo:
        print("To finish setup:\n")
        for i, t in enumerate(todo, 1):
            print(f"{i}. {t}\n")
        return 1
    if not reg:
        print("Setup is complete. Open a paper with its Overleaf editor URL:\n"
              f"    python3 {Path(__file__).resolve()} open "
              "https://www.overleaf.com/project/<24-hex-id> --name mypaper\n"
              "Then run /pdf-review in the directory it prints.")
    else:
        print("Setup is complete. Run /pdf-review in a project directory, or "
              "open another paper with `open <editor-url>`.")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("login"); p.add_argument("--token"); p.add_argument("--from-remote")
    p.set_defaults(fn=cmd_login)
    p = sub.add_parser("secure"); p.add_argument("dir"); p.set_defaults(fn=cmd_secure)
    p = sub.add_parser("session"); p.add_argument("--cookie")
    p.add_argument("--clear", action="store_true"); p.set_defaults(fn=cmd_session)
    p = sub.add_parser("open"); p.add_argument("target"); p.add_argument("--name")
    p.add_argument("--dir"); p.set_defaults(fn=cmd_open)
    p = sub.add_parser("build"); p.add_argument("--dir")
    p.add_argument("--mode", choices=["local", "overleaf"], default="local")
    p.add_argument("--push-first", action="store_true"); p.add_argument("-m", "--message")
    p.set_defaults(fn=cmd_build)
    p = sub.add_parser("push"); p.add_argument("--dir")
    p.add_argument("-m", "--message")
    p.add_argument("--force-assets", action="store_true")
    p.set_defaults(fn=cmd_push)
    p = sub.add_parser("setup"); p.set_defaults(fn=cmd_setup)
    p = sub.add_parser("list"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("_askpass"); p.add_argument("prompt", nargs="?", default="")
    p.set_defaults(fn=cmd_askpass)
    p = sub.add_parser("vendor-bst"); p.add_argument("--dir"); p.set_defaults(fn=cmd_vendor_bst)

    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    raise SystemExit(main())
