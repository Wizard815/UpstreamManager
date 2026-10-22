"""JSON-file persistence for Upstream Management.

Single source of truth lives in `data/state.json` (override with the
UPSTREAM_STATE_FILE env var). It holds tracked repos + their chat/upstream state,
and the app's own settings, including its LLM connection (any OpenAI-compatible
server: llama-swap, llama-server, Argos, ...). It never touches hermes config.
"""
from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

from . import config

_STATE: dict | None = None

def _ensure() -> dict:
    global _STATE
    if _STATE is None:
        _STATE = config.load()
    return _STATE


def load_state() -> dict[str, Any]:
    return _ensure()


def save_state() -> None:
    config.save(_ensure())


# --------------------------------------------------------------------------- repos
def all_repos() -> list[dict]:
    return _ensure().get("repos", [])


def get_repo(repo_id: str) -> dict | None:
    for r in all_repos():
        if r.get("id") == repo_id:
            return r
    return None


def add_repo(repo: dict) -> dict:
    repos = _ensure()["repos"]
    repos.append(repo)
    save_state()
    return repo


def save_repo(repo: dict) -> dict:
    repos = _ensure()["repos"]
    if repo.get("deleted") or repo.get("id") in {r.get("id") for r in repos if r.get("deleted")}:
        _ensure()["repos"] = [r for r in repos if r.get("id") != repo.get("id")]
        save_state()
        return repo
    for i, existing in enumerate(repos):
        if existing.get("id") == repo.get("id"):
            repos[i] = repo
            break
    else:
        repos.append(repo)
    save_state()
    return repo


def remove_repo(repo_id: str) -> bool:
    repos = _ensure()["repos"]
    _ensure()["repos"] = [r for r in repos if r.get("id") != repo_id]
    save_state()
    return True


def upsert_repo(repo: dict) -> dict:
    """Add a new repo or refresh an existing one with the same id.

    Fields the caller does not set (chat sessions, indexed_commits, ...) are kept,
    so re-adding a repo never wipes its history or re-indexes its commits.
    """
    for i, existing in enumerate(_ensure()["repos"]):
        if existing.get("id") == repo.get("id"):
            repo = {**existing, **repo}
            _ensure()["repos"][i] = repo
            save_state()
            return repo
    _ensure()["repos"].append(repo)
    save_state()
    return repo


# --------------------------------------------------------------------------- settings / LLM
def settings() -> dict:
    return _ensure().setdefault("settings", {})


def save_settings(patch: dict) -> dict:
    s = settings()
    s.update(patch)
    save_state()
    return s


def load_llm_config() -> dict:
    """Return the app's own LLM connection: {base_url, model, api_key}."""
    return {"base_url": "http://127.0.0.1:8080/v1", "model": "", "api_key": "", **settings().get("llm", {})}


def save_llm_config(patch: dict) -> dict:
    s = settings()
    s.setdefault("llm", {}).update(patch)
    save_state()
    return s["llm"]


# --------------------------------------------------------------------- requests
def requests_store() -> list[dict]:
    return _ensure().setdefault("requests", [])


def get_requests() -> list[dict]:
    return requests_store()


def record_request(rec: dict) -> dict:
    requests_store().append(rec)
    save_state()
    return rec


def get_request(req_id: str) -> dict | None:
    for r in requests_store():
        if r.get("id") == req_id:
            return r
    return None


def decide_request(req_id: str, decision: str, comment: str | None = None) -> dict | None:
    for r in requests_store():
        if r.get("id") == req_id:
            r["status"] = "approved" if decision == "approve" else "denied"
            r["comment"] = comment
            r["decided_at"] = config.now_iso()
            save_state()
            return r
    return None


# --------------------------------------------------------------------- chat
# Each repo keeps a list of chat sessions. Starting a new session resets what
# the LLM sees of the conversation; long-term memory (memory.py) is unaffected.
def _sessions(repo: dict) -> list[dict]:
    sessions = repo.setdefault("chat_sessions", [])
    legacy = repo.pop("chat_history", None)
    if legacy:  # pre-session state: keep old messages as their own session
        sessions.insert(0, {"id": "s-imported", "title": "Imported history",
                            "created_at": legacy[0].get("timestamp") or config.now_iso(),
                            "messages": legacy})
        repo.setdefault("active_session", "s-imported")
    return sessions


def list_sessions(repo_id: str) -> list[dict]:
    repo = get_repo(repo_id)
    if not repo:
        return []
    active = repo.get("active_session")
    return [{"id": s["id"], "title": s.get("title"), "created_at": s.get("created_at"),
             "count": len(s.get("messages", [])), "active": s["id"] == active}
            for s in _sessions(repo)]


def new_session(repo_id: str) -> dict:
    repo = get_repo(repo_id)
    sessions = _sessions(repo)
    # abandoned empty chats are clutter: drop them when starting another
    sessions[:] = [s for s in sessions if s.get("messages")]
    sid = f"s-{uuid.uuid4().hex[:8]}"
    session = {"id": sid, "title": None, "created_at": config.now_iso(), "messages": []}
    sessions.append(session)
    repo["active_session"] = sid
    save_state()
    return session


def delete_session(repo_id: str, session_id: str) -> bool:
    """Delete one chat (its messages only; long-term memory is untouched)."""
    repo = get_repo(repo_id)
    sessions = _sessions(repo)
    kept = [s for s in sessions if s["id"] != session_id]
    if len(kept) == len(sessions):
        return False
    sessions[:] = kept
    if repo.get("active_session") == session_id:
        repo["active_session"] = kept[-1]["id"] if kept else None
    save_state()
    return True


def get_session(repo_id: str, session_id: str | None = None) -> dict:
    """The requested session, else the active one, else a fresh one."""
    repo = get_repo(repo_id)
    want = session_id or repo.get("active_session")
    for s in _sessions(repo):
        if s["id"] == want:
            return s
    return new_session(repo_id)


def set_active_session(repo_id: str, session_id: str) -> dict:
    session = get_session(repo_id, session_id)
    get_repo(repo_id)["active_session"] = session["id"]
    save_state()
    return session


def add_chat_message(repo_id: str, session_id: str, role: str, content: str, error: bool = False,
                     attachments: list | None = None) -> dict:
    session = get_session(repo_id, session_id)
    msg = {"role": role, "content": content, "timestamp": config.now_iso()}
    if attachments:
        msg["attachments"] = attachments
    if error:
        msg["error"] = True
    session["messages"].append(msg)
    if not session.get("title") and role == "user":
        session["title"] = content.strip().splitlines()[0][:60]
    save_state()
    return msg
