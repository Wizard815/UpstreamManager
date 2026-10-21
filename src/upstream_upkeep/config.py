"""Configuration and persistence for Upstream Management.

State (tracked repos, detected changes, approvals, chat history) is kept in a
single JSON file under the data dir so the app is easy to back up and to inspect.
"""
from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Project root is three levels up from this file (src/upstream_upkeep/config.py).
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("UPSTREAM_STATE_FILE", PROJECT_ROOT / "data" / "state.json"))

_lock = threading.RLock()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load() -> dict[str, Any]:
    with _lock:
        if not CONFIG_PATH.exists():
            return {"repos": [], "settings": {}}
        try:
            return json.loads(CONFIG_PATH.read_text())
        except json.JSONDecodeError:
            return {"repos": [], "settings": {}}


def save(state: dict[str, Any]) -> None:
    with _lock:
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=False))
        os.replace(tmp, CONFIG_PATH)


def repo_state(repos: list[dict], repo_id: str) -> dict | None:
    for r in repos:
        if r["id"] == repo_id:
            return r
    return None


def set_repo_state(repos: list[dict], repo_id: str, patch: dict) -> list[dict]:
    for r in repos:
        if r["id"] == repo_id:
            r.update(patch)
            return repos
    return repos


@dataclass
class Repo:
    id: str
    path: str
    name: str | None
    upstream_url: str | None
    base_ref: str | None
    # last known HEAD sha on the base ref
    last_sha: str | None = None
    upstream_sha: str | None = None
    recent_sha: str | None = None
    last_checked: str | None = None
    # detected change ids since last_checked (local vs upstream vs recent)
    changes: list[str] = field(default_factory=list)
    # daily run state
    last_daily_run: str | None = None
    pending_pushes: list[dict] = field(default_factory=list)
    chat_history: list[dict] = field(default_factory=list)
    last_commit_sha: str | None = None
    # approval history (commit message, sha, decided, decided_at)
    approval_history: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Repo":
        return cls(**{k: v for k, v in d.items() if k in [f for f in cls.__dataclass_fields__]})
