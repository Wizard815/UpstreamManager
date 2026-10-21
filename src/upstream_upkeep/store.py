"""JSON-file persistence for Upstream Management.

Single source of truth lives in `data/state.json` (override with the
UPSTREAM_STATE_FILE env var). It holds tracked repos + their chat/upstream state,
and the LLM/settings block that mirrors the hermes-agent config.yaml so the app
shares the same backend as Studio.
"""
from __future__ import annotations

import json
import os
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
    """Add a new repo or replace an existing one with the same id."""
    for i, existing in enumerate(_ensure()["repos"]):
        if existing.get("id") == repo.get("id"):
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
    """Return the LLM/provider block that mirrors hermes config.yaml."""
    return settings().get("llm", {
        "provider": "custom:llama-swap",
        "base_url": "http://192.168.20.5:8642/v1",
        "model": "ornith-1.5-35b-q6_k",
        "api_mode": "chat_completions",
    })


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
def get_chat_history(repo_id: str) -> list[dict]:
    repo = get_repo(repo_id)
    return repo.get("chat_history", []) if repo else []


def add_chat_message(repo_id: str, role: str, content: str) -> dict:
    repo = get_repo(repo_id)
    if not repo:
        return {}
    hist = repo.setdefault("chat_history", [])
    msg = {"role": role, "content": content, "timestamp": config.now_iso()}
    hist.append(msg)
    save_state()
    return msg
