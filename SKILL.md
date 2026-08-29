---
name: herdr-run
description: Launch long-running background processes (web servers, LLM proxies, training jobs, eval runs, daemons, watchers) as live, user-controllable foreground processes inside a dedicated Herdr "background" workspace. Creates the workspace and {purpose}_N tabs automatically, places each process in a pane (max 4 per tab in a 2x2 grid, then a new tab), names panes with their listening ports, tees all output to logs/{purpose}/, and records every launch in a global registry. Use whenever the user asks to start, run, launch, or deploy any server, service, training, or eval process that should keep running where they can watch and control it (启动服务, 后台跑训练, 部署代理). Not for one-shot commands — use plain Bash for those.
---

# herdr-run

Launch a persistent process the way the user actually wants long-running work to run: **visible in a terminal pane, running in the foreground of that pane, logging to disk, and interruptible with Ctrl-C** — not hidden in a detached shell. The `herdr_run.py` script does all placement work in one call; never orchestrate tabs/panes by hand for this.

## When to use

Use when launching anything that keeps running and matters over time: API/LLM proxy servers, web servers, training jobs (SFT/RL/rollout), evaluation runs, daemons, file watchers, notebooks.

Do **not** use for: one-shot commands (tests, builds, greps), anything expected to finish in seconds, or work the user asked to keep inside the current pane. For general herdr inspection/control unrelated to launching processes, the plain `herdr` skill applies instead.

## Quick start

```bash
python3 <skill-dir>/scripts/herdr_run.py launch <purpose> "<command>" [--note "..."] [--port N]
python3 <skill-dir>/scripts/herdr_run.py list            # what's running, where
```

`<skill-dir>` is this skill's base directory (shown when the skill loads). Example:

```bash
python3 ~/.claude/skills/herdr-run/scripts/herdr_run.py launch llm_proxy \
  "uv run python -m proxy.server --port 7890" \
  --port 7890 --note "vllm api proxy"
```

The script prints JSON with `pane_id`, `tab_label`, `log`, and follow-up command hints. Use those IDs verbatim for everything afterwards.

## Purpose names are a shared registry — check before you create

Purpose names (the `llm_proxy` in `llm_proxy_1`) are the vocabulary the user sees in tabs and log directories forever. They are the user's namespace, not yours to invent freely. Protocol:

1. Run `herdr_run.py list --purposes` to see the registered purposes and how many launches each has.
2. Reuse an existing purpose when it is the same *kind* of process — a second proxy server is another `llm_proxy` launch, not `proxy2`.
3. For a genuinely new kind of process, **ask the user to name it** (or propose a name and get their nod — if they already named it in their request, that counts). Then launch with `--new-purpose`.
4. The script rejects unknown purposes without `--new-purpose`, listing what is registered — treat that error as "go ask", never as "find a way around".

The registry has no file of its own: known purposes are derived from the launch registry (`<skill>/data/launches.jsonl`, override with `HERDR_RUN_RECORD_FILE` / `--record-file`) plus every live `{purpose}_n` tab. Editing that JSONL edits the vocabulary.

## What you must decide before launching

- **purpose** — a short slug for the *kind* of process: `llm_proxy`, `rollout`, `sft`, `eval`. It names the tab (`llm_proxy_1`) and the log directory. Lowercase `[a-z0-9_-]`.
- **--note** — a human-readable phrase for the pane title, e.g. "vllm api proxy". Defaults to the first words of the command; a good note is better.
- **--port N** — **required whenever the process listens on a port.** Scan the command for it: `--port N`, `-p N`, `PORT=N`, `--listen`, `:8xxx` in a URL. The port is appended to the pane name (`vllm api proxy:7890`) so the user can spot conflicts at a glance. Repeat the flag for multiple ports. If you discover the port only after startup, rename: `herdr pane rename <pane_id> "<label>:<port>"`.
- **--cwd** — where the command runs; defaults to the current working directory. Pass it explicitly when the process must run from its project root and you are elsewhere.
- **--focus** — only when the user asked to watch it start; default keeps their focus untouched.

## Verify, follow, and control

After launching, confirm the process came up instead of assuming:

```bash
herdr pane wait-output <pane_id> --match "Uvicorn running" --timeout 60000
herdr pane read <pane_id> --source recent-unwrapped
```

For servers, prefer verifying by connectivity over waiting for a startup banner: many programs print no banner at all (Python 3.14's `http.server` doesn't) or emit it on stderr. Both stdout and stderr are already tee'd to the log. If the first check races the startup, read the pane once, then re-check.

```bash
curl -s localhost:<port>/                                    # any platform
lsof -nP -iTCP:<port> -sTCP:LISTEN                           # macOS / Linux
Get-NetTCPConnection -LocalPort <port> -State Listen         # Windows PowerShell
```

Note: on herdr 0.8.2 `pane read --lines N` returns empty output — do not pass `--lines`. Output longer than the pane's screen is in the log file anyway.

Everything the process prints is also on disk at the reported `log` path — prefer reading the file for long output. To stop a process, send it a Ctrl-C (it is a foreground process in that pane):

```bash
herdr pane send-keys <pane_id> ctrl+c
```

To see everything currently running (and recently finished), read the joined view instead of raw files:

```bash
python3 <skill-dir>/scripts/herdr_run.py list              # live processes
python3 <skill-dir>/scripts/herdr_run.py list --history    # everything ever
python3 <skill-dir>/scripts/herdr_run.py list --purposes   # registered purposes
python3 <skill-dir>/scripts/herdr_run.py list --json       # machine-readable
```

Each row joins a registry entry with live herdr state: `running` (foreground process in pane), `idle` (pane back at shell — process exited), `superseded` (pane reused by a newer launch), `gone` (pane closed). It also flags untracked panes that live in the background workspace but were not launched through this skill.

## Placement rules (what the user sees)

- Everything lands in one workspace labeled `background`, created on first use.
- Tabs are named `{purpose}_{n}`. A tab holds at most **4 panes in a 2x2 grid**; the 5th process of the same purpose opens `{purpose}_{n+1}`.
- The pane title is the note plus ports. Idle-looking placement is handled automatically — do not pre-split panes yourself.

## Options

| Option | Default | Meaning |
|---|---|---|
| `--note TEXT` | first words of command | pane title; also the log filename slug |
| `--port N` (repeatable) | none | listening port(s), shown in pane title |
| `--cwd PATH` | current dir | working directory of the process |
| `--log-dir PATH` | `<cwd>/logs` | log root; file lands in `<log-dir>/<purpose>/<yymmdd-hhmmss-note>.log` (`-2` suffix on same-second collisions) |
| `--new-purpose` | off | register a genuinely new purpose — only after asking the user |
| `--workspace-label` | `background` (env `HERDR_RUN_WORKSPACE`) | target workspace |
| `--record-file PATH` | `<skill>/data/launches.jsonl` (env `HERDR_RUN_RECORD_FILE`) | global registry |
| `--no-record` | off | skip the registry entry (the purpose then only stays known via its live tabs) |
| `--focus` | off | focus the new tab |
| `--dry-run` | off | print the placement plan, change nothing |

`list` options: `--purposes` (purpose registry instead of processes), `--history` (include exited/superseded), `--json`, plus the same `--workspace-label` / `--record-file`.

## Edge cases

- **Very long or heavily-quoted commands** (heredocs, multi-line): write them to a script file (e.g. `run_server.sh`) and launch `bash run_server.sh` instead — `pane run` types the command into a real shell.
- **PowerShell quoting on Windows**: put multi-line commands or commands with nested quotes in a `.ps1` file and launch `powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\path\job.ps1`. Logs are written as UTF-8 on every platform.
- **Launching several processes of the same purpose**: do it sequentially, one script call at a time — placement is decided from live tab/pane state, so parallel same-purpose launches can race and land in the wrong tab. Different purposes never collide and may run in parallel.
- **Interactive prompts** (confirmation, login) will block the pane; read the pane and answer via `herdr pane send-text <pane_id> "<answer>"` or pick non-interactive flags upfront.
- **Processes that daemonize themselves** (`&`, `nohup`, daemon mode) defeat the design — the pane would go idle while the real process hides. Strip backgrounding and let the pane be the process's lifetime.
- The script refuses nothing silently: any herdr error is reported with the failing command. `--dry-run` is cheap — use it when unsure about placement.
- **Platforms**: macOS, Linux, and Windows all work. The logging pipeline is generated per platform. Windows PowerShell uses an explicit UTF-8 writer and a per-launch running marker so nested PowerShell commands are not mistaken for an idle pane.
