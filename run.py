"""Start Flock: python run.py [--open]

Serves http://127.0.0.1:8780 (KCH_PORT) to this computer only. --open also opens it in your browser.
Data: KCH_HOME (default ~/.flock). Setup: python install.py. More: README.md."""
import os
import sys
import threading
import webbrowser

if sys.version_info < (3, 11):
    sys.exit(f"Flock needs Python 3.11 or newer (this is {sys.version.split()[0]}).")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Started from a shell inside a Claude Code session (or a harness chat)? That session's own variables must not reach the
# Claude Code sessions this app starts: one that inherits CLAUDE_PID and the messaging pipe takes itself for that
# session's child, and a test that did so ended the session it came from. Settings (KCH_HOME, KCH_PROFILE, KCH_PORT,
# CLAUDE_CODE_GIT_BASH_PATH...) stay; only the per-session ones go.
SESSION_VARS = ("CLAUDECODE", "CLAUDE_PID", "CLAUDE_EFFORT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
                "CLAUDE_CODE_SESSION_ATTENDED", "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN",
                "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_SSE_PORT")
for _k in list(os.environ):
    if _k.upper() in SESSION_VARS or _k.upper().startswith(("KCH_AGENT_", "KCH_COMPUTER_")):
        del os.environ[_k]

from harness import config, server  # noqa: E402

def wait_for(pid):
    """--after PID (a restart from Settings > Updates): wait until that copy has exited and freed the port."""
    import socket
    import time
    from harness import osutil
    t0 = time.time()
    while osutil.alive(pid) and time.time() - t0 < 60:
        time.sleep(0.3)
    while time.time() - t0 < 90:
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", config.PORT))
                return
            except OSError:
                time.sleep(0.3)


if __name__ == "__main__":
    if "--after" in sys.argv[1:]:
        i = sys.argv.index("--after")
        wait_for(int(sys.argv[i + 1]))
        del sys.argv[i:i + 2]
    if "--open" in sys.argv[1:]:
        threading.Timer(1.5, lambda: webbrowser.open(f"http://127.0.0.1:{config.PORT}/")).start()
    server.main()
