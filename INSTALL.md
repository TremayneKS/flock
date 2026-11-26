# Installing Flock, step by step

Written so that a person or an AI coding agent can follow it exactly. Every step ends with a check. If a check fails,
fix that step before going on. Nothing here needs administrator rights except installing missing system packages.

Agents: run each command, read its output, and compare it with the check. Do not invent keys or sign in to anything
for the user: steps that need the user (pasting an API key, signing in to Claude or ChatGPT) are marked **(user)**.

## 1. Prerequisites

| | Needed for | Check | Install if missing |
|---|---|---|---|
| Python 3.11+ | the app | `python --version` (or `python3 --version`) prints 3.11 or newer | Windows: `winget install Python.Python.3.12` · macOS: `brew install python@3.12` · Debian/Ubuntu: `sudo apt install python3 python3-venv` |
| git | cloning, updates | `git --version` | Windows: `winget install Git.Git` · macOS: `xcode-select --install` · Linux: `sudo apt install git` |
| bash | the agent's Bash tool | Windows: `"C:\Program Files\Git\bin\bash.exe" --version` · macOS/Linux: `bash --version` | Windows: comes with Git for Windows (above) |
| Node.js 20+ | the Browser tool | `node --version` prints v20 or newer | Windows: `winget install OpenJS.NodeJS.LTS` · macOS: `brew install node` · Linux: nodejs.org or NodeSource (Ubuntu 24.04's own `nodejs` is 18, too old) |
| ripgrep (optional) | faster Grep | `rg --version` | Windows: `winget install BurntSushi.ripgrep.MSVC` · macOS: `brew install ripgrep` · Linux: `sudo apt install ripgrep` |

On Windows, open a new terminal after installing anything so PATH is refreshed. Below, `python` means the Python 3.11+
from this table (use `python3` where that is its name).

Notes:
- Windows: do not use WSL's bash or the Microsoft Store's "python" stub. If `python --version` opens the Store, install
  Python from python.org or winget and reopen the terminal.
- The app finds Git Bash by itself. If bash is somewhere unusual, set `FLOCK_BASH` (or `KCH_BASH`) to its full path.

## 2. Get the code

```bash
git clone https://github.com/TremayneKS/flock.git
cd flock
```

Check: `ls` (or `dir`) shows `install.py`, `run.py` and the `harness` folder.

## 3. Run the installer

```bash
python install.py
```

It prints one line per part and ends with `Flock is ready` or `Flock is NOT ready`.

- `OK`: set up and working.
- `INFO`: optional and not set up (for example the Claude or ChatGPT command-line tools). Fine to leave.
- `WARN`: a tool the agent normally has is missing (web search, Browser, bash). The line says how to fix it; fix it,
  then run `python install.py` again.
- `FAIL`: the app cannot run. Fix it and run again. Exit code is 1 in this case, 0 otherwise.

What it does: makes a Python virtual environment in `~/.flock/tools/search-venv` with exactly the packages in
`deps/search.lock` (pinned versions, each download checked against its sha256), installs `@playwright/mcp` from
`deps/browser/package-lock.json` (`npm ci`, each package checked) into `~/.flock/tools/node_modules`,
downloads Playwright's Chromium for the Browser tool only if neither Chrome nor Edge is installed (several hundred MB,
in Playwright's own cache), then starts the app on a spare port and stops it again. About 250 MB in all otherwise.

Options: `--check` (report only), `--with-dictation` (speech to text for the mic button, live as you speak: faster-whisper, plus
NVIDIA's CUDA libraries when an NVIDIA GPU is present; the Whisper models, small for the live words and large-v3-turbo for
the final text, about 2 GB together, download on first use. Without it, Settings > General can switch the mic to the
browser's own speech recognition, which sends your audio to Google's servers),
`--with-meetings` (Meetings for the Meetings button, see below), `--no-search`, `--no-browser`, `--no-test`.

### Optional: meetings (`--with-meetings`)

```bash
python install.py --with-meetings
```

Meetings (in `meetings/`) follows a meeting live: a transcript of your microphone and of what the computer plays,
rough notes as people talk, an assistant per project that remembers every meeting, a proper summary at the end, and
recorded meetings imported from a video or audio file. The installer puts its speech engine (OpenWhisper, MIT, pinned to   <!-- publish-scan: ok -->
one release) in `~/.flock/tools/meetings-engine` and a Python 3.12 virtual environment with that engine's pinned
packages in `~/.flock/tools/meetings-venv` (about 1.5 GB; NVIDIA's CUDA libraries are added when an NVIDIA GPU is
present). It needs Python 3.12 somewhere on the computer (on Windows not the Microsoft Store one): if the Python running
`install.py` is not 3.12 it looks for `python3.12`, `py -3.12` or `uv`, and with `uv` it installs 3.12 itself. On Linux
it also needs PortAudio for the microphone: `sudo apt install libportaudio2` (Fedora: `sudo dnf install portaudio`).

Check: the `Meetings` line says `OK` and how many microphones it found. Then the top bar of the app has a
**Meetings** button. Nothing else to set up: no profile needed.

- Models download the first time they are needed, with progress shown in the app: Whisper large-v3-turbo (1.6 GB) on an
  NVIDIA GPU, Whisper small (480 MB) on the CPU, and a 26 MB voice model for telling speakers apart.
- The assistant, notes and summaries use Claude Code with your Claude plan: install it (`npm i -g @anthropic-ai/claude-code`)
  and sign in once with `claude auth login` **(user)**. Without it, transcripts still work and the app says what is missing.
- Your meetings are kept in `~/.flock/meetings` (one folder per project). Live audio is never saved.
- System audio: on Windows it records what the speakers play. The speech engine also supports Linux (PulseAudio or
  PipeWire's monitor) and macOS (screen recording permission), but live capture there has not been tested with Meetings yet; importing a recorded meeting works the same everywhere.
- Optional settings (your name for the assistant, a different Whisper model, another data folder or port) go in a
  JSON file named in the profile, `"meetings": {"settings": "~/meetings-settings.json"}`; `meetings/settings.py`
  lists every key.

`python install.py --check` reports every part (and exits 1 only if the app itself cannot run). Set `FLOCK_TOOLS` (or `KCH_TOOLS`) to
put the tools folder somewhere else (for example a faster disk).

Linux only: without Chrome or Edge the Browser tool uses Playwright's Chromium, which needs some system libraries. If
the installer's Browser line is a WARN naming missing libraries, run the `sudo ... install-deps chromium` command it
prints, once.

## 4. Start the app

```bash
python run.py --open
```

It serves `http://127.0.0.1:8780` to this computer only and opens it in the default browser (`--open`). Leave the
terminal open; Ctrl+C stops it. To run it in the background instead:

- Windows (no console window): `start "" pythonw run.py` in cmd, or `Start-Process pythonw run.py` in PowerShell
- macOS/Linux: `nohup python3 run.py > ~/.flock/run.log 2>&1 &`

Check (in another terminal):

```bash
curl -s http://127.0.0.1:8780/api/state
```

prints JSON starting with `{"config":`. A different port: set `FLOCK_PORT` (or `KCH_PORT`) before starting (the app also uses the next
port up for previews).

## 5. Connect a model

Open the app, **Settings > Connections > Add**, choose a preset, fill in what it asks, **Save**. The model menu at the
top shows it; send a message to try it.

| You have | Choose | Then |
|---|---|---|
| a Claude subscription | appears by itself as "Claude (your plan, via Claude Code)" once Claude Code is installed | **(user)** run `claude` once in a terminal and sign in |
| a ChatGPT subscription | appears by itself as "ChatGPT (your plan, via Codex)" once Codex is installed | **(user)** run `codex login` once |
| an API key | the provider's preset (DeepSeek, OpenAI, OpenRouter, OrcaRouter, Gemini, Groq, Mistral, Together) | **(user)** paste the key in the form, or set the environment variable the preset names (e.g. `DEEPSEEK_API_KEY`) before starting the app |
| a local server | Ollama, LM Studio, llama.cpp, vLLM | start the server first (the preset's help shows the command); no key |
| anything else OpenAI-compatible | Custom | its `/v1` base address, model name, key if it needs one |

Install the subscription tools with `npm i -g @anthropic-ai/claude-code` and `npm i -g @openai/codex`, then restart the
app so it detects them.

Agents setting up a connection without the UI (no key involved, e.g. a local Ollama):

```bash
curl -s -X POST http://127.0.0.1:8780/api/endpoints -H "X-Flock-App: 1" -H "Content-Type: application/json" \
  -d '{"endpoint": {"preset": "ollama", "name": "Ollama", "model": "qwen3:8b"}}'
```

Check: the response is JSON with `"id"`. Do not put API keys on a command line (shell history keeps them): let the
user paste keys into Settings, or use an environment variable.

## 6. Check it end to end

In the app, start a chat in a scratch folder and send:

> Make a file hello.txt that says hi, then read it back.

Check: the chat shows a Write and a Read step and the file exists in that folder. If WebSearch and Browser were set
up, also try "Search the web for today's weather in London and open the first result".

## 7. Optional: a profile for this computer

Only needed for things the Settings page cannot know: a model server for the app to start and stop, a key you
already keep in a file, extra PATH folders, more folders the agent must never read, a line about you for the system
prompt. Copy [examples/profile.example.json](examples/profile.example.json) to `~/.flock/profile.json`, keep the
parts you need, restart the app. Keys never go in it.

## Updating

In the app: **Settings > Updates**. It checks GitHub on start and every 12 hours (a switch turns that off), shows
what's new, and **Update** installs it: a fast-forward `git pull` for a clone (refused, with nothing changed, if you
have local changes), or a checked download with a backup (**Roll back** undoes it) for a zip install. Then
**Restart now**. Your data in `~/.flock` and your profile are untouched by updates.

By hand:

```bash
git pull
python install.py
```

Check: Settings > Updates says "Up to date", and `python install.py --check` ends with `Flock is ready`.

## Uninstalling

Stop the app, delete the repository folder and `~/.flock` (that removes your chats, settings, keys and the
installed tools). Playwright's Chromium, if it was downloaded, is in Playwright's cache (`%LOCALAPPDATA%\ms-playwright`
on Windows, `~/Library/Caches/ms-playwright` on macOS, `~/.cache/ms-playwright` on Linux).

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Flock needs Python 3.11 or newer` | run it with a newer Python (`python3.12 run.py`) |
| "no bash found" when the agent runs a command (Windows) | install Git for Windows, or set `FLOCK_BASH` (or `KCH_BASH`) |
| the Browser tool is missing from Settings > Tools | `python install.py` (needs Node.js 18+) |
| WebSearch is missing | `python install.py`; see its WARN line |
| a connection says "needs a key" | Settings > Connections > the connection > paste the key |
| "port in use" | another copy is running, or set `FLOCK_PORT=8790` (or `KCH_PORT=8790`) |
| errors | `~/.flock/logs/errors.log` |
| the Meetings button is missing | `python install.py --with-meetings`, then restart the app |
| "Meetings stopped while starting" or "did not start" | `~/.flock/logs/meetings.log`; then `python install.py --with-meetings` again |
| "port 8770 is used by another program" | close that program, or set `"meetings": {"port": 8772}` in the profile |
| "Transcription could not start" in a meeting | the reason is on the banner; details in `~/.flock/meetings/.app/capture.log` |
