"""Run the Upstream Management web app.

    uv run python -m upstream_upkeep.run   # or:  python run.py

Served by Waitress (a production WSGI server). It listens on 127.0.0.1 only,
because the app has no login and can read repos and run git: set HOST=0.0.0.0
(or a LAN address) only when other machines must reach it directly, and only on
a trusted network. A reverse proxy on the same machine works with the default.
Waitress runs one process with a thread pool, which keeps the in-process
locks around the JSON state file and the daily check valid.

Env: PORT (default 5000), HOST (default 127.0.0.1), UPSTREAM_THREADS (default 8),
UPSTREAM_STATE_FILE to override the state file location.
"""
import os

from waitress import serve

from . import memory
from .app import flask_app


def main(default_port: int = 5000) -> None:
    memory.start_background()  # writes the log of each finished day that had activity
    serve(
        flask_app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", str(default_port))),
        threads=int(os.environ.get("UPSTREAM_THREADS", "8")),
        # LLM chat and git fetches can run for minutes; don't drop them early
        # (keep this above UPSTREAM_LLM_TIMEOUT).
        channel_timeout=int(os.environ.get("UPSTREAM_LLM_TIMEOUT", "900")) + 60,
    )


if __name__ == "__main__":
    main()
