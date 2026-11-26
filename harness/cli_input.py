"""Holds a Claude Code process's input so it outlives the app (claude_engine.turn starts the CLI through this).

    python cli_input.py <inbox> <events file> <errors file> -- claude -p --input-format stream-json ...

Claude Code runs background subagents (Agent with run_in_background) and background shell commands inside its own
process: they live exactly as long as it does. Its input used to be a pipe from the app, so

- after a restart the new copy of the app had no way to write into it, and ended it to answer the user's next message
  (23 Nov 2026, 13:37 and 14:09: four background agents and a background command stopped each time), and
- once the input was shut, the next message could only go in through a new process, the old one ended for it.

This small process is the CLI's parent and holds its input instead. Any copy of the app writes a message by appending
one stream-json line to the inbox file; it is passed on as it comes. The line END closes the CLI's input (it then
ends when its turn and its background work are done). It exits with the CLI's exit code, and ending this process's
tree (Stop) ends the CLI and everything it started. Standard library only; started with a console Python."""
import os
import subprocess
import sys
import time

END = b'{"type": "kch_input_end"}'
TERM = b'{"type": "kch_terminate_cli"}'
TICK = 0.05


def main(argv):
    if "--" not in argv or argv.index("--") != 3:
        sys.stderr.write("usage: cli_input.py <inbox> <events file> <errors file> -- <command...>\n")
        return 2
    inbox, out_f, err_f, cmd = argv[0], argv[1], argv[2], argv[4:]
    flags = {"creationflags": 0x08000000} if os.name == "nt" else {}       # CREATE_NO_WINDOW; same group as this one
    with open(out_f, "ab") as fo, open(err_f, "ab") as fe:
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=fo, stderr=fe, **flags)
    buf, shut = b"", False
    with open(inbox, "ab"):
        pass                                                # the app made it; never fail on a missing one
    with open(inbox, "rb") as f:
        while proc.poll() is None:
            chunk = f.readline()
            if not chunk:
                time.sleep(TICK)
                continue
            buf += chunk
            if not buf.endswith(b"\n"):                     # a line still being written
                continue
            line, buf = buf, b""
            stripped = line.strip()
            if stripped == TERM:
                # Turn already completed, but CLI hung (e.g. background server kept handles open):
                # terminate ONLY the CLI process itself, never its detached children.
                try:
                    proc.terminate()
                except OSError:
                    pass
                try:
                    proc.wait(timeout=3.0)
                except subprocess.TimeoutExpired:
                    try:
                        proc.kill()
                    except OSError:
                        pass
                break
            if shut:
                continue
            try:
                if stripped == END:
                    proc.stdin.close()
                    shut = True
                elif stripped:
                    proc.stdin.write(line)
                    proc.stdin.flush()
            except OSError:                                 # the CLI has closed its input or is ending
                shut = True
    try:
        return proc.wait(timeout=3.0)
    except subprocess.TimeoutExpired:
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
