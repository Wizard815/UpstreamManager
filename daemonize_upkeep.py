#!/usr/bin/env python3
"""Daemonize Upstream-Keep so it persists independently of the launching shell.

Mirrors how Hermes Studio's own node server survives (PPID=1, fully detached):
this wrapper does an os.fork() + setsid + second fork so the resulting Flask
process is reparented to init and no longer tied to any tool-runtime session.
Usage:
    python daemonize_upkeep.py            # starts on :5001 with default state file
    UPSTREAM_STATE_FILE=/path PORT=9000 python daemonize_upkeep.py
"""
import os
import sys


def daemonize():
    """Double-fork into background, redirect stdio to a log file."""
    # First fork.
    if os.fork() > 0:
        os._exit(0)
    os.setsid()
    # Second fork.
    if os.fork() < 0:
        os._exit(1)

    # Detach file descriptors from the controlling terminal.
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)

    log = open("/tmp/upkeep-daemon.log", "a")
    os.dup2(log.f, 1)
    os.dup2(log.file, 2)
    return log


def main():
    try:
        from upstream_upkeep.run import flask_app
    except Exception as exc:  # pragma: no cover - startup failure
        sys.stderr.write(f"[daemon] failed to import app: {exc}\n")
        os._exit(1)

    port = int(os.environ.get("PORT", "5001"))
    print(
        f"[daemon] Upstream-Keep starting on :{port} (pid={os.getpid()}, "
        f"state_file={os.environ.get('UPSTREAM_STATE_FILE')})",
        flush=True,
    )
    # debug=False so we don't get the reloader's extra fork loop; werkzeug
    # re-exec is not needed for a long-lived daemon.
    flask_app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    daemonize()
    main()
