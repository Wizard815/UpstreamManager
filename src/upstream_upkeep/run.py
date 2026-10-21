"""Run the Upstream Management web app.

    uv run python -m upstream_upkeep.run   # or:  python run.py

Served by Waitress (a production WSGI server) on 0.0.0.0 so the app is
reachable directly as a website as well as through Studio's /upstream proxy.
Waitress runs one process with a thread pool, which keeps the in-process
locks around the JSON state file and the daily check valid.

Env: PORT (default 5000), HOST (default 0.0.0.0), UPSTREAM_THREADS (default 8),
UPSTREAM_STATE_FILE to override the state file location.
"""
import os

from waitress import serve

from .app import flask_app

if __name__ == "__main__":
    serve(
        flask_app,
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "5000")),
        threads=int(os.environ.get("UPSTREAM_THREADS", "8")),
        # LLM chat and git fetches can run for minutes; don't drop them early.
        channel_timeout=300,
    )
