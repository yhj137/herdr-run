#!/usr/bin/env python3
"""herdr-run: launch persistent foreground processes into a dedicated herdr
"background" workspace, with automatic tab/pane placement, tee logging, and
a global launch registry.

Usage:
    python3 herdr_run.py launch <purpose> "<command>" [options]
    python3 herdr_run.py list [--history] [--json]

Placement rules:
    - All processes live in a workspace labeled `background` (created on
      first use).
    - Tabs are named `{purpose}_{n}` (llm_proxy_1, rollout_2, ...).
    - A tab holds at most 4 panes arranged as a 2x2 grid; a 5th process
      opens `{purpose}_{n+1}`.
    - The process runs in the pane's foreground (piped through `tee`), so
      the user can watch it live and interrupt it with Ctrl-C at any time.

Purpose registry:
    - Purpose names are a shared vocabulary between the agent and the user,
      so a brand-new purpose requires explicit consent: the launch is
      rejected with the list of known purposes unless `--new-purpose` is
      passed (the agent should have asked the user first).
    - Known purposes = every purpose in the launch registry plus every
      purpose inferred from live `{purpose}_{n}` tabs.
"""

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime
from uuid import uuid4

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_WORKSPACE_LABEL = os.environ.get("HERDR_RUN_WORKSPACE", "background")
# The global launch registry lives in the skill's own data directory so it
# travels with the skill; HERDR_RUN_RECORD_FILE / --record-file override it.
DEFAULT_RECORD_FILE = os.environ.get(
    "HERDR_RUN_RECORD_FILE", os.path.join(SKILL_DIR, "data", "launches.jsonl")
)
ON_WINDOWS = sys.platform == "win32"
MAX_PANES_PER_TAB = 4

HERDR_BIN = shutil.which("herdr")
if HERDR_BIN is None:
    print("herdr-run: error: `herdr` not found in PATH - install it from "
          "https://herdr.dev first", file=sys.stderr)
    sys.exit(1)


def die(msg, code=1):
    print(f"herdr-run: error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"herdr-run: warning: {msg}", file=sys.stderr)


def run_herdr_process(args):
    """Run Herdr with deterministic text decoding on every platform.

    Herdr emits UTF-8 JSON.  Relying on the Windows system code page (often
    GBK) can crash before we get a chance to report the actual CLI error.
    """
    return subprocess.run(
        [HERDR_BIN, *args], capture_output=True, text=True,
        encoding="utf-8", errors="replace")


def herdr(*args):
    """Run a herdr CLI command and return its parsed JSON result."""
    proc = run_herdr_process(args)
    if proc.returncode != 0:
        detail = proc.stderr.strip() or proc.stdout.strip()
        die(f"`herdr {' '.join(args)}` failed (exit {proc.returncode}): {detail}")
    out = proc.stdout.strip()
    if not out:
        return {}
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        die(f"`herdr {' '.join(args)}` returned non-JSON output: {out[:200]}")


def herdr_try(*args):
    """Like herdr(), but returns None instead of exiting on failure
    (e.g. querying a pane that has since closed)."""
    proc = run_herdr_process(args)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None


def sanitize_purpose(raw):
    purpose = re.sub(r"[^a-z0-9_-]+", "_", raw.lower()).strip("_")
    if not purpose:
        die(f"purpose {raw!r} has no usable characters (need [a-z0-9_-])")
    return purpose


def pane_label(note, ports):
    # Any process that listens on a port must carry that port in its pane
    # name, e.g. "llm proxy:28117", so port conflicts are visible at a glance.
    if ports:
        return f"{note}:{','.join(str(p) for p in ports)}"
    return note


def default_note(command):
    return " ".join(command.split()[:3])[:32]


def note_slug(note, fallback):
    """Turn the pane note into a filename-safe slug for the log name.

    Keeps unicode (Chinese etc.), letters, digits, dots and dashes; turns
    whitespace into dashes; drops everything else.
    """
    slug = re.sub(r"\s+", "-", note.strip())
    slug = re.sub(r"[^\w.-]+", "", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-.")
    return slug[:40] or fallback


def ps_quote(text):
    """Quote a literal for PowerShell (single quotes, '' escaping)."""
    return "'" + text.replace("'", "''") + "'"


def build_shell_command(cwd, banner_prefix, command, log_path):
    """One shell line that cds, prints the banner and runs the command with
    all output tee'd to the log - in the pane shell's own dialect.

    POSIX panes (macOS/Linux) get a brace group piped through tee; Windows
    panes run PowerShell.  Windows PowerShell 5.1's Tee-Object creates UTF-16
    files, so use a UTF-8 StreamWriter while still echoing every line live.
    """
    if ON_WINDOWS:
        return (
            f"Set-Location {ps_quote(cwd)}; if ($?) {{ "
            f"$herdrUtf8 = New-Object System.Text.UTF8Encoding($false); "
            f"$herdrWriter = New-Object System.IO.StreamWriter("
            f"{ps_quote(log_path)}, $true, $herdrUtf8); "
            f"try {{ "
            f"$herdrBanner = {ps_quote(banner_prefix)} + "
            f"(Get-Date).ToString('o'); "
            f"Write-Output $herdrBanner; "
            f"$herdrWriter.WriteLine($herdrBanner); $herdrWriter.Flush(); "
            f"& {{ {command} }} 2>&1 | ForEach-Object {{ "
            f"Write-Output $_; $herdrWriter.WriteLine($_.ToString()); "
            f"$herdrWriter.Flush() }} "
            f"}} finally {{ $herdrWriter.Dispose() }} }}"
        )
    return (
        f"cd {shlex.quote(cwd)} && "
        # `%Y-%m-%dT%H:%M:%S%z` is supported by both GNU date (Linux) and
        # BSD date (macOS); GNU-only `date -Iseconds` breaks on macOS.
        f"{{ echo {shlex.quote(banner_prefix)}"
        f"$(date '+%Y-%m-%dT%H:%M:%S%z'); "
        f"{command}; }} 2>&1 | tee -a {shlex.quote(log_path)}"
    )


def read_registry(record_file):
    """All launch-registry entries, oldest first."""
    entries = []
    try:
        with open(record_file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    warn(f"skipping malformed registry line: {line[:80]}")
    except FileNotFoundError:
        pass
    return entries


def find_workspace(label):
    result = herdr("workspace", "list")
    for ws in result.get("result", {}).get("workspaces", []):
        if ws.get("label") == label:
            return ws
    return None


def tabs_of_workspace(workspace_id):
    result = herdr("tab", "list", "--workspace", workspace_id)
    return result.get("result", {}).get("tabs", [])


def purpose_tabs(tabs, purpose):
    """Tabs managed for this purpose: label matches {purpose}_{n}."""
    pattern = re.compile(rf"^{re.escape(purpose)}_(\d+)$")
    managed = []
    for tab in tabs:
        m = pattern.match(tab.get("label", ""))
        if m:
            managed.append((int(m.group(1)), tab))
    managed.sort(key=lambda pair: pair[0])
    return managed


def known_purposes(record_file, workspace_label):
    """Every purpose ever launched (registry) plus every live {purpose}_n tab."""
    purposes = {e.get("purpose") for e in read_registry(record_file)
                if e.get("purpose")}
    ws = find_workspace(workspace_label)
    if ws:
        for tab in tabs_of_workspace(ws["workspace_id"]):
            m = re.match(r"^(.*)_(\d+)$", tab.get("label", ""))
            if m and m.group(1):
                purposes.add(m.group(1))
    return {p for p in purposes if p}


def panes_of_workspace(workspace_id):
    result = herdr("pane", "list", "--workspace", workspace_id)
    return result.get("result", {}).get("panes", [])


def panes_of_tab(workspace_id, tab_id):
    return [p for p in panes_of_workspace(workspace_id)
            if p.get("tab_id") == tab_id]


def pane_layout_rects(any_pane_id):
    """Rect geometry for every pane in the tab that owns any_pane_id."""
    result = herdr("pane", "layout", "--pane", any_pane_id)
    layout = result.get("result", {}).get("layout", {})
    return {p["pane_id"]: p["rect"] for p in layout.get("panes", [])
            if "rect" in p}


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def recent_lines(text, n=8, width=120):
    """The last n non-empty lines of a pane screen or log tail - ANSI
    stripped and width-capped.  Empty lines are skipped so trailing blanks
    never hide the output that matters."""
    lines = [ANSI_RE.sub("", ln).rstrip() for ln in (text or "").splitlines()]
    lines = [ln for ln in lines if ln.strip()]
    return [ln[:width - 1] + "…" if len(ln) > width else ln
            for ln in lines[-n:]]


def pane_recent(pane_id, n=8):
    """Recent screen segment of a live pane.  A shell prompt in it means
    the process exited; scrolling output means it is busy.  Liveness is
    read off the screen by the agent, not computed."""
    proc = run_herdr_process(
        ["pane", "read", pane_id, "--source", "recent-unwrapped"])
    if proc.returncode != 0:
        return []
    return recent_lines(proc.stdout, n)


def log_recent(log_path, n=8):
    """Tail segment of the on-disk log, for rows whose pane is gone."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            fh.seek(max(0, fh.tell() - 16384))
            return recent_lines(fh.read().decode("utf-8", "replace"), n)
    except OSError:
        return []


def choose_split(rects):
    """Pick (pane_id, direction) so the tab converges on a 2x2 grid.

    n=1: split right -> two columns.
    n=2: split the left column's top pane down -> left column gets 2 rows.
    n=3: split the right column's top pane down -> full 2x2.
    """
    n = len(rects)
    if n == 0:
        return None, "right"
    if n == 1:
        return next(iter(rects)), "right"

    def largest():
        pid, rect = max(rects.items(),
                        key=lambda kv: kv[1]["width"] * kv[1]["height"])
        return pid, ("right" if rect["width"] >= rect["height"] else "down")

    if n >= MAX_PANES_PER_TAB:
        # Oversubscribed (manual edits or a race); shouldn't normally happen.
        return largest()

    xs = sorted({r["x"] for r in rects.values()})
    if n == 2:
        if len(xs) >= 2:
            left = [pid for pid, r in rects.items() if r["x"] == xs[0]]
            top = min(left, key=lambda pid: rects[pid]["y"])
            return top, "down"
        top = min(rects, key=lambda pid: rects[pid]["y"])  # stacked rows
        return top, "right"
    # n == 3: split the top pane of the rightmost column.
    right = [pid for pid, r in rects.items() if r["x"] == xs[-1]]
    if not right:
        return largest()
    top = min(right, key=lambda pid: rects[pid]["y"])
    return top, "down"


# --------------------------------------------------------------------------
# launch
# --------------------------------------------------------------------------

def cmd_launch(args):
    command = args.command.strip()
    if not command:
        die("no command given")

    purpose = sanitize_purpose(args.purpose)
    note = (args.note or default_note(command)).strip() or purpose
    cwd = os.path.abspath(args.cwd)
    if not os.path.isdir(cwd):
        die(f"--cwd does not exist: {cwd}")
    log_dir = os.path.abspath(args.log_dir or os.path.join(cwd, "logs"))

    # ---- purpose registry gate ---------------------------------------------
    record_file = os.path.abspath(args.record_file)
    known = known_purposes(record_file, args.workspace_label)
    if purpose not in known and not args.new_purpose:
        listing = ", ".join(sorted(known)) or "(none registered yet)"
        die(
            f"purpose '{purpose}' is not registered.\n"
            f"  known purposes: {listing}\n"
            f"  reuse a known purpose if this is the same kind of process, or "
            f"ask the user to name the new one and pass --new-purpose. "
            f"Purpose names are the shared tab/log vocabulary - creating one "
            f"is the user's call, not the agent's."
        )

    stamp = datetime.now().strftime("%y%m%d-%H%M%S")
    # Log name carries the note so the file says what process it belongs to:
    # yymmdd-hhmmss-<note>.log (a numeric -2, -3... suffix resolves same-second
    # collisions of the same note).
    base = f"{stamp}-{note_slug(note, purpose)}"
    log_name = f"{base}.log"
    n = 2
    while os.path.exists(os.path.join(log_dir, purpose, log_name)):
        log_name = f"{base}-{n}.log"
        n += 1
    log_path = os.path.join(log_dir, purpose, log_name)
    label = pane_label(note, args.port)
    focus_flag = "--focus" if args.focus else "--no-focus"

    # ---- Phase 1: read state and compute a plan (no mutations) -----------
    plan = {
        "purpose": purpose, "note": note, "ports": args.port,
        "pane_label": label, "cwd": cwd, "log": log_path, "command": command,
        "create_workspace": False, "create_tab": False,
        "split": None,  # (pane_id, direction) when splitting
        "reuse_root": None,  # fresh root pane of a workspace/tab we created
        "workspace_id": None, "tab_id": None, "tab_label": None,
    }

    ws = find_workspace(args.workspace_label)
    if ws is None:
        plan["create_workspace"] = True
        plan["tab_label"] = f"{purpose}_1"
    else:
        ws_id = ws["workspace_id"]
        plan["workspace_id"] = ws_id
        tabs = tabs_of_workspace(ws_id)
        managed = purpose_tabs(tabs, purpose)
        target = next((t for _, t in managed
                       if t.get("pane_count", 0) < MAX_PANES_PER_TAB), None)
        if target is None:
            next_n = managed[-1][0] + 1 if managed else 1
            plan["create_tab"] = True
            plan["tab_label"] = f"{purpose}_{next_n}"
        else:
            plan["tab_id"] = target["tab_id"]
            plan["tab_label"] = target["label"]
            panes = panes_of_tab(ws_id, target["tab_id"])
            if not panes:
                die(f"tab {plan['tab_label']} ({target['tab_id']}) has no "
                    "panes; it may be mid-close, retry")
            # Every launch gets a fresh pane.  An existing pane belongs to
            # its launch for as long as the pane exists, even after the
            # process exited - closing the pane is how its slot is freed.
            # This also keeps placement purely structural (tab pane counts),
            # never dependent on eventually-consistent process state, so
            # back-to-back launches cannot race on "is this pane idle?".
            rects = pane_layout_rects(panes[0]["pane_id"])
            split_id, direction = choose_split(rects)
            if split_id is None:
                die(f"no pane geometry available for tab {plan['tab_label']}")
            plan["split"] = [split_id, direction]

    # ---- Phase 2: execute the plan ----------------------------------------
    if args.dry_run:
        plan["dry_run"] = True
        plan["record_file"] = record_file if not args.no_record else None
        print(json.dumps(plan, indent=2, ensure_ascii=False))
        return

    if plan["create_workspace"]:
        result = herdr("workspace", "create", "--label",
                       args.workspace_label, focus_flag)
        res = result.get("result", {})
        ws_id = res.get("workspace", {}).get("workspace_id")
        tab = res.get("tab", {})
        root_pane = res.get("root_pane", {})
        if not ws_id or not tab.get("tab_id") or not root_pane.get("pane_id"):
            die(f"unexpected workspace create response: {result}")
        plan["workspace_id"] = ws_id
        herdr("tab", "rename", tab["tab_id"], plan["tab_label"])
        plan["tab_id"] = tab["tab_id"]
        plan["reuse_root"] = root_pane["pane_id"]
    elif plan["create_tab"]:
        result = herdr("tab", "create", "--workspace", plan["workspace_id"],
                       "--label", plan["tab_label"], "--cwd", cwd, focus_flag)
        res = result.get("result", {})
        tab_id = res.get("tab", {}).get("tab_id")
        root_pane_id = res.get("root_pane", {}).get("pane_id")
        if not tab_id or not root_pane_id:
            die(f"unexpected tab create response: {result}")
        plan["tab_id"] = tab_id
        plan["reuse_root"] = root_pane_id
    elif plan["split"]:
        split_id, direction = plan["split"]
        result = herdr("pane", "split", split_id, "--direction", direction,
                       "--ratio", "0.5", "--cwd", cwd, focus_flag)
        pane_id = result.get("result", {}).get("pane", {}).get("pane_id")
        if not pane_id:
            die(f"unexpected pane split response: {result}")
        plan["pane_id"] = pane_id
    if plan.get("pane_id") is None:
        plan["pane_id"] = plan["reuse_root"]
    if not plan["pane_id"]:
        die("internal error: no target pane resolved")

    # ---- Phase 3: name the pane and start the process ----------------------
    launch_id = uuid4().hex
    banner_prefix = (f"[herdr-run] launch={launch_id} purpose={purpose} "
                     f"pane={label} log={log_path} started=")
    shell_command = build_shell_command(
        cwd, banner_prefix, command, log_path)

    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    herdr("pane", "rename", plan["pane_id"], label)
    herdr("pane", "run", plan["pane_id"], shell_command)

    # Wait for the *executed* banner so an immediate failure (pane not at a
    # prompt, typo in cd, ...) surfaces now instead of silently.  Matching
    # "started=2" rather than the launch id is what makes this execution
    # proof: the tty echoes the typed keystrokes back even when nothing
    # runs, but the echo shows the unexpanded `'$(date ...)'` literal while
    # a real execution prints the expanded timestamp (started=2026-...).
    # The wait also covers a freshly created pane whose shell is still
    # booting - the line only executes once the shell is ready.
    banner_probe = subprocess.run(
        [HERDR_BIN, "pane", "wait-output", plan["pane_id"],
         "--match", "started=2", "--timeout", "10000"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    if banner_probe.returncode != 0:
        die(f"launch banner never executed within 10s; the command was "
            f"not recorded as started. Inspect it: herdr pane read "
            f"{plan['pane_id']} --source recent-unwrapped")

    # ---- Phase 4: global launch registry -----------------------------------
    record_path = None
    if not args.no_record:
        os.makedirs(os.path.dirname(record_file), exist_ok=True)
        entry = {
            "time": datetime.now().astimezone().isoformat(timespec="seconds"),
            "purpose": purpose,
            "command": command,
            "note": note,
            "ports": args.port,
            "pane": plan["pane_id"],
            "tab": plan["tab_id"],
            "tab_label": plan["tab_label"],
            "workspace": plan["workspace_id"],
            "workspace_label": args.workspace_label,
            "cwd": cwd,
            "log": log_path,
            "launch_id": launch_id,
            "caller_pane": os.environ.get("HERDR_PANE_ID"),
        }
        with open(record_file, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        record_path = record_file

    print(json.dumps({
        "purpose": purpose,
        "pane_label": label,
        "workspace_id": plan["workspace_id"],
        "tab_id": plan["tab_id"],
        "tab_label": plan["tab_label"],
        "pane_id": plan["pane_id"],
        "log": log_path,
        "record_file": record_path,
        "command": command,
        "hint": f"follow output: herdr pane wait-output {plan['pane_id']} "
                f"--match <text> | read: herdr pane read {plan['pane_id']} "
                f"--source recent-unwrapped | "
                f"stop: herdr pane send-keys {plan['pane_id']} ctrl+c | "
                f"overview: python3 herdr_run.py list",
    }, indent=2, ensure_ascii=False))


# --------------------------------------------------------------------------
# list
# --------------------------------------------------------------------------

def cmd_list(args):
    record_file = os.path.abspath(args.record_file)
    entries = read_registry(record_file)
    ws = find_workspace(args.workspace_label)

    live_panes = {}
    if ws:
        for pane in panes_of_workspace(ws["workspace_id"]):
            pid = pane.get("pane_id")
            if pid:
                live_panes[pid] = pane

    if args.purposes:
        cmd_list_purposes(args, entries, record_file)
        return

    def pane_state(pane_id):
        """live | gone for a registry pane id - purely structural (does the
        pane exist).  Whether the process is actually running is read off
        the RECENT column, not computed."""
        return "live" if pane_id in live_panes else "gone"

    # Group registry entries by pane: the newest entry per pane owns its
    # live state; older entries on the same pane were superseded (legacy
    # data - launches no longer reuse panes).
    by_pane = {}
    for idx, entry in enumerate(entries):
        by_pane.setdefault(entry.get("pane"), []).append((idx, entry))

    rows = []
    for pane_id, group in by_pane.items():
        newest_idx, newest = group[-1]
        state = pane_state(pane_id) if pane_id else "gone"
        for idx, entry in group[:-1]:
            rows.append((idx, entry, "superseded"))
        rows.append((newest_idx, newest, state))
    rows.sort(key=lambda r: r[0], reverse=True)  # newest launches first

    tracked_panes = {e.get("pane") for e in entries}
    untracked = []
    for pid, pane in live_panes.items():
        if pid not in tracked_panes:
            info = herdr_try("pane", "get", "--pane", pid)
            untracked.append({
                "pane_id": pid,
                "tab_id": pane.get("tab_id"),
                "label": (info or {}).get("result", {}).get("pane", {})
                .get("label", ""),
                "recent": pane_recent(pid, args.lines),
            })

    def fmt_row(idx, e, state):
        ports = ",".join(str(p) for p in e.get("ports") or []) or "-"
        recent = (pane_recent(e.get("pane"), args.lines)
                  if state == "live" else log_recent(e.get("log") or "",
                                                     args.lines))
        return {
            "time": e.get("time", "?"),
            "purpose": e.get("purpose", "?"),
            "note": e.get("note", "?"),
            "ports": ports,
            "pane": e.get("pane") or "-",
            "tab": e.get("tab_label") or "-",
            "state": state,
            "recent": recent,
            "log": e.get("log", "-"),
            "command": e.get("command", "-"),
        }

    live_states = {"live"}
    shown = [r for r in rows if args.history or r[2] in live_states]
    out_rows = [fmt_row(*r) for r in shown]

    if args.json:
        print(json.dumps({
            "workspace": ws.get("workspace_id") if ws else None,
            "processes": out_rows,
            "untracked_panes": untracked,
            "record_file": record_file,
        }, indent=2, ensure_ascii=False))
        return

    if not out_rows:
        print(f"no background processes "
              f"({'registry empty' if not entries else 'none alive'}; "
              f"registry: {record_file})")
        return

    headers = ["STATE", "PURPOSE", "NOTE", "PORTS", "PANE", "TAB", "LOG"]
    table = [[r["state"], r["purpose"], r["note"], r["ports"], r["pane"],
              r["tab"], r["log"]] for r in out_rows]
    widths = [max(len(h), *(len(row[i]) for row in table))
              for i, h in enumerate(headers)]
    for row in ([headers] + table):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))

    if args.lines:
        print()
        for r in out_rows:
            ports = f":{r['ports']}" if r["ports"] != "-" else ""
            print(f"── {r['pane']} · {r['note']}{ports} "
                  f"({r['tab']}, {r['state']})")
            for ln in r["recent"]:
                print(f"   {ln}")
            if not r["recent"]:
                print("   (no recent output)")

    if untracked:
        print("\nuntracked panes in workspace (not launched via herdr-run):")
        for u in untracked:
            print(f"── {u['pane_id']}"
                  + (f" ({u['label']})" if u["label"] else "") + " (untracked)")
            for ln in u["recent"]:
                print(f"   {ln}")
    print(f"\nstate: live=pane 仍在 | superseded=同 pane 旧记录(遗留) | "
          f"gone=pane 已关闭 · 屏幕段=最近 {args.lines} 行非空输出"
          f"（--lines N 调整，0 关闭）——段里出现 shell 提示符即空闲，"
          f"持续滚动即在跑")


def cmd_list_purposes(args, entries, record_file):
    """The purpose registry the launch gate checks: every purpose ever
    launched plus every live {purpose}_n tab."""
    stats = {}
    for e in entries:
        p = e.get("purpose")
        if not p:
            continue
        s = stats.setdefault(p, {"launches": 0, "live": 0, "last": ""})
        s["launches"] += 1
        if e.get("time", "") > s["last"]:
            s["last"] = e.get("time", "")
    # Live tabs are the authoritative "live" count and also cover purposes
    # that were never recorded (--no-record) or whose registry was lost.
    ws = find_workspace(args.workspace_label)
    if ws:
        for tab in tabs_of_workspace(ws["workspace_id"]):
            m = re.match(r"^(.*)_(\d+)$", tab.get("label", ""))
            if m:
                stats.setdefault(
                    m.group(1), {"launches": 0, "live": 0, "last": ""})
                stats[m.group(1)]["live"] += tab.get("pane_count", 0)
    if args.json:
        print(json.dumps({
            "purposes": [
                {"purpose": p, "launches": s["launches"],
                 "live_panes": s["live"], "last_used": s["last"]}
                for p, s in sorted(stats.items())
            ],
            "record_file": record_file,
        }, indent=2, ensure_ascii=False))
        return
    if not stats:
        print(f"no purposes registered yet (registry: {record_file})")
        return
    headers = ["PURPOSE", "LAUNCHES", "LIVE PANES", "LAST USED"]
    rows = [[p, str(s["launches"]), str(s["live"]), s["last"] or "-"]
            for p, s in sorted(stats.items())]
    widths = [max(len(h), *(len(r[i]) for r in rows))
              for i, h in enumerate(headers)]
    for row in ([headers] + rows):
        print("  ".join(c.ljust(widths[i]) for i, c in enumerate(row)))


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        prog="herdr_run.py",
        description="Launch persistent foreground processes in the herdr "
                    "'background' workspace with logging and placement.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_launch = sub.add_parser(
        "launch", help="start a process in the background workspace")
    p_launch.add_argument("purpose", help="short slug naming this kind of "
                          "process, e.g. llm_proxy, rollout, sft; must be "
                          "known or explicitly registered with --new-purpose")
    p_launch.add_argument("command", help="full command line to run, as ONE "
                          "quoted string")
    p_launch.add_argument("--note", help="short pane label describing this "
                          "process; also names the log file "
                          "(default: derived from the command)")
    p_launch.add_argument("--port", type=int, action="append", default=[],
                          help="port the process listens on; repeatable. "
                          "Always pass this when the command listens on a "
                          "port: it is appended to the pane name as :PORT")
    p_launch.add_argument("--cwd", default=os.getcwd(),
                          help="working directory for the process "
                               "(default: current directory)")
    p_launch.add_argument("--log-dir", default=None,
                          help="log root (default: <cwd>/logs; logs land in "
                               "<log-dir>/<purpose>/yymmdd-hhmmss-note.log)")
    p_launch.add_argument("--new-purpose", action="store_true",
                          help="register this purpose as new; only pass it "
                          "after asking the user to name a genuinely new "
                          "kind of process")
    p_launch.add_argument("--workspace-label",
                          default=DEFAULT_WORKSPACE_LABEL,
                          help="herdr workspace to place processes in "
                               "(default: background)")
    p_launch.add_argument("--record-file", default=DEFAULT_RECORD_FILE,
                          help="global launch registry, JSONL (default: "
                               "<skill>/data/launches.jsonl or "
                               "$HERDR_RUN_RECORD_FILE)")
    p_launch.add_argument("--no-record", action="store_true",
                          help="do not append to the global launch registry "
                               "(also removes the purpose from the registry)")
    p_launch.add_argument("--focus", action="store_true",
                          help="focus the new tab (default: keep the user's "
                               "focus unchanged)")
    p_launch.add_argument("--dry-run", action="store_true",
                          help="print the plan as JSON and change nothing")
    p_launch.set_defaults(func=cmd_launch)

    p_list = sub.add_parser(
        "list", help="list background processes: registry joined with live "
                     "herdr state")
    p_list.add_argument("--purposes", action="store_true",
                        help="list registered purpose names instead of "
                             "processes (the registry the launch gate "
                             "checks)")
    p_list.add_argument("--history", action="store_true",
                        help="include exited/superseded launches, not just "
                             "live ones")
    p_list.add_argument("--json", action="store_true",
                        help="machine-readable output")
    p_list.add_argument("--lines", type=int, default=8,
                        help="lines of recent screen output shown per "
                             "process, 0 to hide the segments (default: 8)")
    p_list.add_argument("--workspace-label", default=DEFAULT_WORKSPACE_LABEL,
                        help="herdr workspace to inspect (default: background)")
    p_list.add_argument("--record-file", default=DEFAULT_RECORD_FILE,
                        help="launch registry (default: "
                             "<skill>/data/launches.jsonl or "
                             "$HERDR_RUN_RECORD_FILE)")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
