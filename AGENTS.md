# Flock: notes for coding agents working on this repository

A local desktop web app (`python run.py`, http://127.0.0.1:8780, project previews on 8781) that runs a coding agent against any
model: the native Anthropic Messages API, any OpenAI-compatible API, OpenCode Go, or the Claude Code / Codex / Antigravity CLIs
with the user's own plan.
User-facing docs: README.md, INSTALL.md, examples/profile.example.json.

## Layout
- `harness/` Python 3.11+, **standard library only** (the app must never need pip).
  - `config.py` paths and settings layers: built-in DEFAULTS <- `profile.json` (hand-written, never written by the app)
    <- `config.json` (only what the user changed in the app). Keys live in `KCH_HOME/keys/<id>`, env vars, files named
    by the profile, or a profile `key_provider` plugin; never in a JSON file.
  - `server.py` HTTP + Server-Sent Events. Listens on 127.0.0.1; Host/Origin checked on every request, the `X-Flock-App: 1` (or legacy `X-KC-App: 1`)
    header required on every change, previews on another port (origin).
  - `agent.py` the loop: system prompt and tool list fixed per chat (prompt cache), compaction, steering (messages sent
    while it works go in before the next step), "carry on" nudges for unfinished turns, waiting on a launcher-backed endpoint.
    The system prompt's "Where you are" section is worked out once per chat (OS and shell, the project folder, this
    app's code folder, URL and data folder, one line per tool the chat has, how to check work, limits); keep it short,
    it costs every request (about 730 tokens with all tools). Tool lines are listed only for tools the chat has.
  - `checks.py` the verification gate (`agent.Gate` / `agent._gate`): when the model ends a turn that wrote code, the
    harness checks the changed files (compile, `node --check`, JSON, HTML/CSS parse) and runs the project's test command
    (`.flock.json` (with fallback to `.nest.json` / `.raven.json` / `.kc-harness.json`) `{"test": ...}` or `Test: ...` in the project notes), shows a `check` message under the reply,
    sends errors back (at most `MAX_FIX` times) and asks once to verify when nothing was run or opened after the last
    change (`runs_something`: Bash that only reads, like `git diff`, does not count). Also `app_code()` / `live_for`:
    a turn that changed this app's own code ends with "reload the page / restart the app" (no approval: the self-edit
    Allow / Deny gate was removed 2026-11-08; the Computer tool keeps its own Allow / Deny in `computer.py`).
  - Steering: a message sent while a turn runs is queued (`agent.start`, item `t` = when) and goes in at the model's next
    step. If a tool call is still running `agent.STEER_GRACE` (3 s) after both the message and the call started, it is
    cut short so the message goes in at once: the harness's Bash kills its child (`ctx.steer`, result `steered`), Claude
    Code gets a stream-json `interrupt` control_request (`claude_engine`), then the message in the same process. Not
    while Claude Code has subagents working: its interrupt ends all of them, so the message is written straight in
    (Claude gets it at its next step) and only the main turn's own long shell command is ended (`stop_task`). Codex
    takes no input mid-turn: its queued messages start the next turn.
  - `providers.py` OpenAI-compatible AND native Anthropic Messages API streaming by hand (http.client, so Stop closes the
    socket), prompt caching, extended thinking, llama.cpp timings, key failover, retries. `presets.py` the templates in
    Settings > Connections > Add (with boolean env-key detection).
  - `subagents.py` the Agent tool & embedded subagent chats: a subagent is an interactive child chat (meta `sub_of`, `depth`)
    run by the normal turn code on any connection, with forked parent context, `ParentContext` search, `ReportToMain`, named
    presets, and live event forwarding (`{"type": "sub"}`). `servers/agents.py` gives Claude Code, Codex, and Antigravity the
    same tool (`harness_agent`, stdio MCP + CLI mode, calling back to POST /api/subagent with an in-memory token).
  - `group_chat.py` Multi-model Group Chat rooms (`mode: "group"`): persistent hidden member chats (`group_member_of`), delta-only
    Room Board delivery, live thinking/tool stream forwarding (`group_stream`), 3-way subagent policy (`off`, `shared_worker` async
    worker lane, `open` max 3), soft turn budget & Pass Baton, unanimous sign-off, anti-loop fuse, and `fork_room_state()` for
    independent Group Chat forking (fresh member & worker chats, cursor=0, remapped `member_chat_id`).
  - `sections.py` editable top-level chat sections (`General`, `Work`, `Personal`), archive/cleanup with undo, and the
    `OrganizeChats` / `harness_chats` tool. `fork.py` full & mid-chat forking across all engines.
  - `burn_monitor.py` live Claude Code usage/burn-rate monitor emitting in-app SSE `burn_alert` banners.
  - `tools.py` Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, Browser, TodoWrite, Notes, Skill, with Claude
    Code's names and argument shapes. Credential stores are fenced off (`DENY` + the profile's `deny_extra` + `keys/`).
  - `subagents.py` the Agent tool: a subagent is a hidden child chat (meta `sub_of`, `depth`) run by the normal turn code
    on any connection, its events forwarded to the parent's window as `{"type": "sub"}`; depth/step/time limits, one slot
    per single-slot server. `servers/agents.py` gives Claude Code and Codex the same tool (`harness_agent`, stdio MCP,
    calls back to POST /api/subagent with an in-memory token).
  - `computer.py` the Computer tool (Claude's computer-use actions) for every model: permission asked in the window
    (`computer_ask` events, web/computer.js; screen once per chat, control once or per chat), the "controlling your
    computer" banner, Stop/Esc (physical Esc polled on Windows/macOS while in control), held keys always released,
    screenshots natively as images, and `gui_automation_refusal` (the Bash guard). Claude Code and
    Codex get it from `servers/computer.py` (stdio MCP, POST /api/computer/call with an in-memory token).
    `desktop.py` the backends, stdlib only: Windows ctypes (SendInput, GDI StretchBlt, per-monitor DPI), macOS Quartz +
    screencapture (untested on a Mac), X11 xdotool + xwd, and a dry run that records input (KCH_COMPUTER_BACKEND=dryrun;
    `x11` + KCH_COMPUTER_WRAP/KCH_COMPUTER_DISPLAY point it at a virtual display). Tests: `tools/test_computer.py`
    (`--x11 <WSL distro>` drives Xvfb). Never test it on the user's real screen.
  - `mcp.py` stdio MCP client. Built in when installed: `web` = `servers/search.py` (own venv, the one non-stdlib part),
    `browser` = Playwright MCP pinned to `PLAYWRIGHT_MCP` (the Browser tool's argument mapping depends on it).
  - `browser_init.js` runs in every page the Browser opens and records text drawn on canvases for the "Canvas check".
    It must never change page behaviour.
  - `runtime.py` optional launcher for the model server behind one connection, described by the profile. Fully detached;
    any later app instance adopts it from `state/runtime.json`. With the profile's `runtime.remote`, a launcher that dies
    leaves its remote GPU on the record as an orphan (state "orphaned", Stop ends it) until the remote side says it is gone.
  - `claude_engine.py`, `codex_engine.py` the Claude Code and Codex CLIs as engines (their own tools, one session per chat).
    Claude Code is started through `cli_input.py`, which holds its input and passes on what the app appends to
    `turns/<chat>/engine.in`: its background agents and commands live inside its process, so new messages (and a copy
    of the app after a restart) write into the same process instead of ending it. A resumed session's empty results
    (num_turns 0, its reports of tasks the last process took with it) end nothing.
  - `resume.py` turns that carry on after the app restarts: a record per running turn in KCH_HOME/turns, picked up at
    start (`agent.resume_turns`) with one line to the model; Bash output and CLI engine events go to files so they
    outlive the app. Restart with POST /api/restart or by ending the server process alone, never its tree. Test:
    `tools/test_resume.py`.
  - `effort.py` reasoning levels: Fast / Think / Deep for API connections (what each sends is in providers.build_body), the
    CLIs' own effort levels for claude-code / codex (`--effort`, `model_reasoning_effort`), per chat in meta `effort`
    ({connection id: level}), the connection's default in its `effort`; every reply's stats carry the level used.
  - `imports.py`, `skills.py` skills and MCP servers linked from Claude Code and Codex (keys never copied).
  - `chat_import.py` chats from ChatGPT and Claude.ai exports and Claude Code / Codex sessions, as background jobs (exports
    read as a stream). Other apps' tool calls are stored as display `parts` plus a short `model_content`.
    "Update imported chats" (`update_imports`: all, a group, a chat; `update_chat` holds the rule for a chat carried on
    here; `IMPORTS/sources.json` says since when a source is followed; Settings auto: off / start / hourly).
  - `meetings.py` the Meeting button: finds Meetings (bundled, or a profile's own meetings app), starts it once, tells
    "not installed", "port taken", "stopped while starting" apart. `meetings_setup.py` its part of install.py.
    `llm.py` the selected model for other local apps (Meetings): `/api/llm/v1/models` and `/chat/completions`
    (OpenAI-style, streaming), 127.0.0.1 only, a per-install token in the key folder (never the connections' keys), no
    tools, never starts a launcher-backed server (answers "off" or waits briefly for one that is starting).
    `dictation.py` + `whisper_worker.py` live dictation in its own venv (the page streams 16 kHz PCM to /api/dictation;
    a small model shows words as they are said, the accurate model polishes each stretch at a pause and after Stop);
    `stream_policy.py` the stdlib streaming policy (LocalAgreement-2, cuts, seams), tested by `tools/test_dictation.py`.
  - `osutil.py` everything that differs between Windows, macOS and Linux (`long_path` for paths past 260 characters).
    `store.py` chats (paged reads for long ones: `Chat.window`). `groups.py` chat groups (KCH_HOME/groups.json + `group`
    in each chat.json); `chat_import.py` also reads the Claude desktop app's own session list (titles, which session a
    fork or side session came from) when it is there, and finds forks from their copied start when it is not.
  - `selfupdate.py` the app updating itself from GitHub (check every 12 h with ETags, git fast-forward or verified
    release zip with backup and rollback, restart). `version.py` VERSION + the repository address (`REPO_URL`,
    written by `tools/publish.py --repo-url`). `updates.py` the tools' update checks (llama.cpp, Playwright, pip).
- `meetings/` Meetings, a separate app (its own venv with the speech engine, Python 3.12, not stdlib-only) copied in
  from its own repository with `tools/sync_meetings.py`: change it there, then sync. `meetings/settings.py` documents
  its settings.json.
- `web/` vanilla JS and CSS, no build step. The editor bundle `web/editor/cm.js` is built from `tools/editor`.
  `sheets.js` + `sheets.css` unified desktop sheets (Model picker, Subagents, Report-to-Main, Sections, Move/Archive),
  See-My-Screen desktop capture, Quick-Connect styling, and narrow-window drawer layout.
  `visuals.js` + `visuals.css` Flock Visuals v1 (zero-dependency CSP-safe inline SVG charts, KPI tiles, sortable tables, steps, callouts).
  `group-chat.js` + `group-chat.css` Multi-model Group Chat UI (sidebar Group button, create/edit modal, collapsible compact bar,
  live inline thinking/tool streaming, model-family styling, trace inspection).
  `chat-list.js` the chat list (groups, nested forks, drag and drop, right-click menus, multi-select).
  `dictation.js` + `dictation-worklet.js` the mic: live text in the composer, or the browser's own recognition (opt-in).
  - `viewers.js` + `viewers.css` the file pane's viewers (images, PDF, media, markdown, tables, JSON/XML trees, notebooks,
    Office, archives, 3D models, molecules, fonts, SQLite, sequences, hex). Libraries in `web/vendor/<lib>/` (version and
    license in each `SOURCE.txt`) load only when a file needs them; `web/vendor/three/three-viewer.js` is built from
    `tools/three`. Nothing a viewed file holds may run as the app: rendered HTML goes through DOMPurify, SVG only as an
    `<img>` data: URL, PDF and HTML from the preview port, and `/api/files/raw` answers with a sandbox CSP.
- `install.py` sets up the tools in `KCH_HOME/tools` (or `KCH_TOOLS`) from the pinned locks in `deps/` and self-tests;
  optional parts register in its `OPTIONAL` list (`--with-NAME`). `run.py` starts the app (`--after PID`: a restart).
- `VERSION` MAJOR.MINOR.PATCH; a release = tag vX.Y.Z + a GitHub release (its notes are the app's "what's new").
- `tools/` `smoke.py` (one turn, no UI), `test_chat_import.py` (chat import parsers, synthetic samples), `test_dictation.py` (live dictation policy, recorded decodes), `test_computer.py` (Computer tool, dry run + Xvfb), `sitebench.py` (website-building tests through the API), `browser_check.py`
  (drive the Browser tool by hand), `publish.py` (scan for secrets and personal details, mirror into a clean public
  repository), `lock_deps.py` (regenerate `deps/`), `sync_meetings.py` (copy Meetings into `meetings/`), `make_icon.py`.

## Rules
- Nothing time-dependent or per-turn in the system prompt or tool list: every step must reuse the cached prefix.
- No hidden extra model calls (titles, summaries): on a single-slot local server they evict the chat's cache.
- Nothing personal or machine-specific in the repository: paths, hosts, names, keys go in a profile (document new
  fields in `examples/profile.example.json`). The model-visible text says "the user", never a name.
- Works on Windows, macOS and Linux: processes through `osutil`, no `shell=True`, expand `~` yourself, no hard-coded
  drive letters.
- Test on a copy, never the user's own data: `KCH_HOME=<a test folder> KCH_PORT=8790 python run.py`. To test changes
  to the app's own code by an agent, set `"app_code": "<a throwaway clone>"` in that test folder's config.json.
- The harness never restarts or stops itself or its model server because a model asked; after edits to live code it
  tells the user to reload or restart.
- A launcher with `"stop": "file"` is stopped by writing its stop file, never killed: it may be holding a paid remote GPU.
- The Bash tool refuses commands that would kill the app, the launcher or every Python process (`tools.kill_guard`; the
  profile adds `protect_commands`). Test runtime changes with a fake launcher on a test copy, never a real GPU.
- Dependencies are pinned: change `requirements-*.txt` or `mcp.PLAYWRIGHT_MCP`, then `python tools/lock_deps.py`.
- Before committing: `python -m py_compile` on changed files, `python install.py --check`, and for agent changes one
  real turn with `tools/smoke.py`.
