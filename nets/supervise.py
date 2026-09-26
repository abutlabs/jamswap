#!/usr/bin/env python3
"""Keep one native node running, as Docker's `restart: unless-stopped` does for a container.

    supervise.py LOG -- COMMAND [ARG ...]

Detaches into a session of its own (no terminal, no caller's process group), runs
COMMAND with stdout and stderr appended to LOG and stdin closed, and restarts it
whenever it exits: after 2 s, doubling up to 30 s while it keeps dying young (< 60 s).
Every restart is logged to LOG as a `[supervise]` line, so a crash stays visible.
SIGTERM or SIGINT stops it: forwarded to COMMAND, which gets 20 s before SIGKILL.
"""
import os
import signal
import subprocess
import sys
import time


def main():
    if "--" not in sys.argv or sys.argv.index("--") != 2 or len(sys.argv) < 4:
        sys.exit(__doc__.split("\n\n")[1])
    log, cmd = sys.argv[1], sys.argv[3:]
    os.setsid()
    state = {"child": None, "stopping": False}

    def stop(signum, frame):
        state["stopping"] = True
        child = state["child"]
        if child is not None and child.poll() is None:
            child.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)

    delay = 2
    while not state["stopping"]:
        started = time.time()
        with open(log, "ab") as out:
            state["child"] = subprocess.Popen(cmd, stdout=out, stderr=subprocess.STDOUT,
                                              stdin=subprocess.DEVNULL)
        code = state["child"].wait()
        if state["stopping"]:
            break
        lived = time.time() - started
        delay = 2 if lived >= 60 else min(delay * 2, 30)
        with open(log, "a") as out:
            out.write("[supervise] %s: exited with %s after %.0f s; restarting in %d s\n"
                      % (time.strftime("%H:%M:%S"), code, lived, delay))
        deadline = time.time() + delay
        while not state["stopping"] and time.time() < deadline:
            time.sleep(0.5)

    child = state["child"]
    if child is not None and child.poll() is None:
        try:
            child.wait(timeout=20)
        except subprocess.TimeoutExpired:
            child.kill()


if __name__ == "__main__":
    main()
