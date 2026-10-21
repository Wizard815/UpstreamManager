"""Run the Upstream Management web app.

    uv run python -m upstream_upkeep.run   # or:  python run.py

Set UPSTREAM_STATE_FILE to override the state file location.
"""
import os

from .app import flask_app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5000"))
    flask_app.run(host="0.0.0.0", port=port, debug=True)
