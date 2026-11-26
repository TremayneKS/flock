# Flock

A local desktop coding agent that runs on your own computer, in a browser window, with whichever model you already have:

- **an API key**: Anthropic API (Claude, native Messages API with prompt caching & extended thinking), OpenAI API, Gemini, DeepSeek, OpenRouter, Groq, Mistral, Together AI, OrcaRouter, or any OpenAI-compatible API
- **your subscription**: Claude (through Claude Code), ChatGPT (through Codex), Gemini (through Antigravity CLI), OpenCode Go
- **a model on your machine or server**: Ollama, LM Studio, llama.cpp, vLLM, or anything that speaks `/v1/chat/completions` with tool calls

The app is one small Python program (standard library only) plus a plain JavaScript page. No account, no telemetry, no
cloud of its own: it talks only to the model you connect and the websites the agent visits.

## What you get

- **Plug in API keys in seconds.** First-run Quick Connect right inside the chat window (and **Settings > Connections > Add**),
  automatic detection of existing environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`,
  `OPENROUTER_API_KEY`, etc.), and per-connection key files in `FLOCK_HOME/keys/` (mode 600, never stored in JSON, never sent back to the browser).
- **Multi-model Group Chats.** Put 2+ models (Claude, ChatGPT/Codex, Gemini, DeepSeek, or local models) in one shared room
  to collaborate on a goal in turns with delta-only Room Board delivery, live inline thinking and tool traces, per-model family
  color styling, soft turn budgets, Pass Baton, live steering, optional asynchronous **Shared Worker** or capped **Open** subagent
  policies (off / 1 shared worker / max 3 open), unanimous sign-off, anti-loop fuse, and full or mid-chat **Group Chat Forking**
  with fresh independent member sessions.
- **Embedded Subagents on any model.** Any chat can delegate work to a subagent on *any* connected model (e.g. a Claude chat
  calling a DeepSeek or local Ollama subagent, or vice versa) via the `Agent` tool (for API/local models) or the `harness_agent`
  MCP server (for Claude Code, Codex, and Antigravity CLIs). Every subagent is a real interactive child chat under its parent:
  open it to watch live progress, chat with it directly while the main chat keeps working, search main-chat history via `ParentContext`,
  save reusable **Named Subagents** with custom roles, and send findings back with `ReportToMain`.
- **Rich inline chat visuals & media.** Models can emit interactive SVG charts (```` ```chart ```` / ```` ```compare ````: bar, hbar,
  line, area, scatter, donut, progress, error bars, confidence bands, reference lines, `null` gaps), KPI stat tiles (```` ```numbers ````),
  sortable searchable tables with inline proportion bars (```` ```table ```` and auto-upgraded Markdown tables with Copy TSV),
  step timelines (```` ```steps ````), and callouts (`[!NOTE]`, `[!CAUTION]`, `[!TIP]`, `[!PREDICTION]`). Backticked local paths to
  images, audio, or video render inline as zoomable lightboxes and media players with a fullscreen video viewer.
- **Chat, files and preview side by side.** A collapsible chat list with editable **Sections** (`General`, `Work`, `Personal`),
  nested groups, multi-select, archive/cleanup with one-click Undo, chat **Forking** across any engine, a file tree you can drag
  files from into the message, a code editor (CodeMirror), and a live preview of the project's pages.
- **A viewer for every common file.** Images (zoom and pan), PDF (view and annotate), video and audio, rendered markdown, CSV and genomics
  tables (VCF, BED, GFF, SAM), JSON and XML trees, Jupyter notebooks, Word, Excel and PowerPoint, ZIP and TAR archives,
  3D models (glTF, STL, OBJ, PLY), molecules (PDB, mmCIF, SDF, MOL2, XYZ), FASTA/FASTQ statistics, fonts, SQLite tables,
  and a hex view for anything else. Text files switch between View and Source. All offline.
- **The tools a coding agent needs:** Read, Write, Edit, Bash, Glob, Grep, WebSearch, WebFetch, a real Browser
  (Playwright: open, click, type, screenshot, console, and a check that reads chart text from canvases), View (native vision),
  Computer (opt-in screen & desktop control with explicit Allow/Deny cards and Esc safety stop), Agent (subagents), TodoWrite,
  Notes, OrganizeChats, and Skills (including the bundled `charts` skill).
- **Mid-turn steering, live activity, and restart resilience.** Send a follow-up while a turn is running to steer it at the next step;
  long commands show live activity and their latest output line; running turns survive app restarts and reconnect cleanly.
- **Fast, Think, Deep (plus native CLI effort levels).** One switch per chat: no thinking, thinking, or deep thinking; Claude Code,
  Codex, and Antigravity connections expose their native effort levels (`Low` to `Max`).
- **Long chats that stay cheap.** The system prompt and tool list never change mid-chat, so providers' prompt caches
  keep hitting. Near the context limit (default 85%) the chat is compacted into a summary that keeps your requests, the
  chat's notes and its turn log.
- **Memory per chat:** a `notes.md` the agent keeps (and you can edit) and a `log.md` of every turn.
- **Imports:** skills and MCP servers from Claude Code and Codex, linked rather than copied (their keys stay put). Drop a ChatGPT or
  Claude.ai data export on the chat list (or Settings > Import), or sync Claude Code and Codex sessions from this computer.
- **Updates itself from GitHub**: checks for a new version on start and every 12 hours (can be turned off), shows what's new,
  and updates in one click with backup and rollback.
- **Optional extras:** local **speech to text** (live streaming Whisper on your computer via `--with-dictation`), a **model-server
  launcher** (start, switch and stop local/remote model servers via `profile.json`), and **Meetings** (built into Flock via `--with-meetings`).

## Quick start

You need **Python 3.11 or newer**. On Windows also **Git for Windows** (its bash runs the agent's commands). For the
Browser tool, **Node.js 20 or newer**.

```bash
git clone https://github.com/TremayneKS/flock.git
cd flock
python install.py
python run.py --open
```

`install.py` sets up web search and the Browser tool inside the app's own data folder, checks everything else, starts
the app once to prove it works, and tells you what (if anything) is missing. Nothing is installed globally (the one
exception: Playwright's Chromium, when the computer has neither Chrome nor Edge). Run it again any time; it skips what
is already done.

Then in the app: pick a provider in the **Quick Connect** card (or **Settings > Connections > Add**), paste your API key
(or let it pick up your environment variable), and start chatting.

Using an AI agent to install it? Point it at [INSTALL.md](INSTALL.md): exact commands per operating system, with a
check after each step.

## Connecting a model

| Group | Choices | What you need |
|---|---|---|
| API key | Anthropic API (Claude), OpenAI API, Gemini, DeepSeek, OpenRouter, Groq, Mistral, Together AI, OrcaRouter | an API key from the provider (or set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `DEEPSEEK_API_KEY`, etc.) |
| Your subscription | Claude (via Claude Code) | `npm i -g @anthropic-ai/claude-code`, then run `claude` once and sign in |
| | ChatGPT (via Codex) | `npm i -g @openai/codex`, then `codex login` once |
| | Gemini (via Antigravity CLI) | install `agy` and sign in |
| | OpenCode Go | an OpenCode Go key |
| Local model | Ollama, LM Studio, llama.cpp, vLLM | the server running (the preset shows the command) |
| Other | Custom OpenAI-compatible | its `/v1` address, model name, key if any |

CLI subscriptions (`claude`, `codex`) appear automatically when installed; they run the agent with that tool's own engine
and your plan's limits. Everything else (including native Anthropic Messages API and OpenAI-compatible APIs) runs through
the app's own agent loop.

Keys you type into Quick Connect or Settings are saved one per file in `FLOCK_HOME/keys/` (mode 600 on macOS and Linux),
never in a settings JSON file, and never sent back to the browser page. You can also use environment variables or a key
file referenced in a local `profile.json`.

## Where things live

| | Default | Change with |
|---|---|---|
| App data: chats, logs, settings, keys, tools | `~/.flock` (keeps existing `~/.nest` / `~/.raven` / `~/.kc-harness`) | `FLOCK_HOME` (or `KCH_HOME`) |
| What `install.py` sets up (venvs, Playwright MCP) | `FLOCK_HOME/tools` | `FLOCK_TOOLS` (or `KCH_TOOLS`) |
| This computer's own setup (optional) | `FLOCK_HOME/profile.json` | `FLOCK_PROFILE` (or `KCH_PROFILE`) |
| Port | `8780` (previews on `8781`) | `FLOCK_PORT` (or `KCH_PORT`) |
| The Bash tool's shell | Git Bash on Windows, `bash` elsewhere | `FLOCK_BASH` (or `KCH_BASH`) |
| Claude Code / Codex / Antigravity CLI | found on PATH | `FLOCK_CLAUDE` / `FLOCK_CODEX` / `FLOCK_AGY` (or `KCH_*`) |

Inside `FLOCK_HOME` (legacy `KCH_HOME` / `RAVEN_HOME` also work): `config.json` (what you changed in the app, only the differences from the defaults), `keys/`,
`chats/<id>/` (`messages.jsonl`, `notes.md`, `log.md`), `logs/`, `workspace/` (default home for new projects),
`tools/` (what `install.py` set up), `cache/`, `browser/`.

**Nothing personal lives in the repository.** Anything specific to one machine (a model server to start, keys kept
elsewhere, extra paths, folders to fence off, a few lines about you for the system prompt) goes in a hand-written
`profile.json` outside it. [examples/profile.example.json](examples/profile.example.json) explains every field.

## Updating

Settings > Updates checks this repository on start and then every 12 hours, in the background (a switch there turns
the automatic checks off; nothing is installed without your click). When there is a new version, a dot appears on
the Settings button and the Updates page shows what's new with an **Update** button:

- **A git clone** follows its branch: Update is `git pull --ff-only`. It refuses, and changes nothing, when you have
  local changes or commits of your own (then pull by hand). Undo goes back to the commit before.
- **A zip download** follows releases: Update downloads the new release from GitHub, checks it, keeps every file it
  replaces in `FLOCK_HOME/updates/`, then copies the new files in. **Roll back** puts the old ones back.

Either way your data (`FLOCK_HOME`: chats, keys, settings) and profile are never touched. **Restart now** starts the
new version on the same port; running turns and model servers carry on across the restart.

## Safety

- The app listens on `127.0.0.1` only. Every request's Host and Origin are checked and every change needs a custom
  header (`X-Flock-App: 1` (legacy `X-KC-App: 1` also accepted)), so a web page open in your browser cannot drive it. Project previews are served from a second
  port so a previewed page cannot reach the app's API either.
- **The agent runs commands as you, with your permissions. There is no sandbox.** Use it on projects you are happy for
  it to change, and keep backups (git). The tools refuse to read common credential stores (SSH keys, cloud CLI
  credentials, browser password stores, `.netrc`, the app's own `keys/`, ...; add your own with `deny_extra`), which
  stops accidents, not a determined attacker.
- With the Claude, ChatGPT, and Antigravity CLI plan connections, those CLIs run their own tools without asking for
  approval (that is how the app drives them). Same caution applies.
- Web pages, search results and files the agent reads can contain instructions aimed at it. Treat what it fetched as
  untrusted, like you would yourself.

## For developers

- `harness/` the server and agent (Python standard library only). `web/` the page (vanilla JS, no build step).
  `harness/servers/search.py` the web research MCP server; `harness/servers/agents.py` the subagents MCP server;
  `harness/servers/computer.py` the desktop Computer tool MCP server. [AGENTS.md](AGENTS.md) (CLAUDE.md imports it) maps
  the code for AI coding agents.
- Tests (all run against isolated temp `KCH_HOME` folders via `tools/testhome.py`, never touching live user data):
  `tools/test_public_release.py`, `tools/test_group_chat.py`, `tools/test_cli_turn_finish.py`, `tools/test_interrupted_tool_calls.py`,
  `tools/test_subagent_tools.py`, `tools/test_visual_rule.py`, `node tools/test_visuals.mjs`,
  `tools/test_fork.py`, `tools/test_steer.py`, `tools/test_resume.py`, `tools/test_chat_import.py`, `tools/test_computer.py`,
  `tools/test_dictation.py`, `tools/test_never_down.py`, `tools/smoke.py`.
- `tools/publish.py scan` checks the repository for secrets, keys, emails, and personal paths.
- Dependencies are pinned in `deps/`; `python tools/lock_deps.py` regenerates them from `requirements-*.txt` and `mcp.PLAYWRIGHT_MCP`.
- The editor bundle `web/editor/cm.js` is prebuilt (`cd tools/editor && npm ci && node build.mjs`); the 3D viewer bundle
  `web/vendor/three/three-viewer.js` is prebuilt (`cd tools/three && npm ci && node build.mjs`).

## License

MIT, see [LICENSE](LICENSE). Bundled third-party code and fonts keep their own licenses: [THIRD_PARTY.md](THIRD_PARTY.md).
