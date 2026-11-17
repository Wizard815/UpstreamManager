"""Hermes: hand a bigger or longer task to a Hermes agent session.

Uses the Hermes gateway's API server (enable it with API_SERVER_ENABLED=true and API_SERVER_KEY in
Hermes; default http://127.0.0.1:8642). Native session endpoints:
  POST /api/sessions                    {title, system_prompt, source}  -> {"session": {"id", ...}}
  POST /api/sessions/{id}/chat          {"message"}                     -> {"message": {"role", "content"}}
  GET  /api/sessions/{id}/messages?order=oldest&limit=..                -> {"data": [...]}
A profile (one per repo if you like, each with its own terminal.cwd) is reached under /p/{profile}/.
The API has no per-session folder, so the session's instructions name the workspace to work in.
Sends run in a background thread: a Hermes turn on a big problem can take many minutes."""
from __future__ import annotations

import os
import threading
import time

import requests

from . import config, credentials, store

TIMEOUT = 15
STATUS_TIMEOUT = 2.5   # a status check must never hold a page up
TURN_TIMEOUT = 3600
_RUNS: dict[str, dict] = {}   # session id -> {"since", "error", "running"}
_LOCK = threading.Lock()


def _why(e: Exception) -> str:
    """A network error in a few words."""
    t = str(e)
    return ("nothing is listening there (is it running?)" if "Connection refused" in t else "timed out" if "timed out" in t.lower()
            else "that name doesn't resolve" if "resolve" in t or "Name or service" in t else type(e).__name__)


class HermesError(Exception):
    pass


def settings() -> dict:
    s = dict(store.settings().get("hermes") or {})
    s.setdefault("url", os.environ.get("HERMES_API_URL", "").strip() or "http://127.0.0.1:8642")
    s.setdefault("profile", os.environ.get("HERMES_API_PROFILE", "").strip())
    s.setdefault("model", os.environ.get("HERMES_API_MODEL", "").strip())
    s.setdefault("enabled", bool(os.environ.get("HERMES_API_URL")))
    return s


def _key() -> str:
    return credentials.get("hermes_api_key") or os.environ.get("HERMES_API_KEY", "").strip() or os.environ.get("API_SERVER_KEY", "").strip()


def _base() -> str:
    s = settings()
    base = s["url"].rstrip("/")
    return base + (f"/p/{s['profile'].strip('/')}" if s["profile"].strip() else "")


def _req(method: str, path: str, timeout: float = TIMEOUT, **kw) -> dict:
    s = settings()
    if not s["enabled"]:
        raise HermesError("Hermes isn't set up: turn it on in Settings → Hermes")
    headers = {"Content-Type": "application/json"}
    if _key():
        headers["Authorization"] = f"Bearer {_key()}"
    try:
        r = requests.request(method, _base() + path, headers=headers, timeout=timeout, **kw)
    except requests.RequestException as e:
        raise HermesError(f"can't reach Hermes at {s['url']} ({_why(e)})") from e
    if r.status_code in (401, 403):
        raise HermesError("Hermes refused the key: save its API_SERVER_KEY under Settings → Hermes")
    if r.status_code >= 400:
        raise HermesError(f"Hermes said {r.status_code}: {r.text[:300]}")
    return r.json() if r.content else {}


def status() -> dict:
    s = settings()
    out = {"enabled": s["enabled"], "url": s["url"], "profile": s["profile"], "model": s["model"], "key_set": bool(_key())}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off"}
    try:
        caps = _req("GET", "/v1/capabilities", timeout=STATUS_TIMEOUT)
        return {**out, "ok": True, "version": caps.get("version") or caps.get("object")}
    except HermesError as e:
        return {**out, "ok": False, "error": str(e)}


def instructions(repo: dict, workdir: str, mcp: bool = True) -> str:
    lines = [
        "You are working on a task handed over from Upstream Manager.",
        f"Work only in this folder: {workdir}" + (" (its own branch; commit there)." if workdir != repo["path"] else "."),
        f"The repo is {repo.get('name') or repo['id']}; its focus branch is {repo.get('focus_branch') or 'main'}.",
        "Never push, force-push or rewrite history. When the change is ready, commit it in that folder and say so:"
        " the user approves it in Upstream Manager.",
    ]
    if mcp:
        lines.append(f"If you have Upstream Manager's MCP tools (server 'upstream'), use them: repo id {repo['id']};"
                     " workspace_status and submit_merge_workspace hand the change back for approval.")
    return "\n".join(lines)


def start(title: str, system_prompt: str) -> str:
    s = settings()
    body = {"title": title[:120], "system_prompt": system_prompt, "source": "upstream-manager"}
    if s["model"]:
        body["model"] = s["model"]
    res = _req("POST", "/api/sessions", json=body)
    sid = (res.get("session") or {}).get("id") or res.get("id")
    if not sid:
        raise HermesError("Hermes made no session")
    return sid


def send(session_id: str, message: str, on_done=None) -> None:
    """Send a message; the reply arrives in the background (poll messages())."""
    with _LOCK:
        if _RUNS.get(session_id, {}).get("running"):
            raise HermesError("Hermes is still working on the last message")
        _RUNS[session_id] = {"running": True, "since": config.now_iso(), "error": None}

    def run():
        err = None
        try:
            _req("POST", f"/api/sessions/{session_id}/chat", timeout=TURN_TIMEOUT, json={"message": message})
        except Exception as e:  # shown to the user with the conversation
            err = str(e)
        with _LOCK:
            _RUNS[session_id] = {"running": False, "since": _RUNS[session_id]["since"], "error": err, "ended": time.time()}
        if on_done:
            try:
                on_done(err)
            except Exception:
                pass

    threading.Thread(target=run, daemon=True, name=f"hermes-{session_id}").start()


def run_state(session_id: str) -> dict:
    with _LOCK:
        return dict(_RUNS.get(session_id) or {"running": False, "error": None})


def messages(session_id: str, limit: int = 200) -> list[dict]:
    res = _req("GET", f"/api/sessions/{session_id}/messages", params={"order": "oldest", "limit": limit})
    out = []
    for m in res.get("data") or []:
        content = m.get("content")
        if isinstance(content, list):  # multimodal parts
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        role = m.get("role") or "assistant"
        if role == "assistant" and not (content or "").strip() and m.get("tool_calls"):
            names = [((c.get("function") or {}).get("name") or c.get("name") or "a tool") for c in m["tool_calls"] if isinstance(c, dict)]
            out.append({"role": "tool", "content": "used " + ", ".join(names), "name": None})
        elif role in ("user", "assistant") and (content or "").strip():
            out.append({"role": role, "content": content, "timestamp": m.get("timestamp") or m.get("created_at")})
        elif role == "tool" and content:
            out.append({"role": "tool", "content": str(content)[:1500], "name": m.get("tool_name") or m.get("name")})
    return out
