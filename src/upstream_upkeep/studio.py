"""Hermes Studio: hand a task to a Studio chat run, so the conversation lives in Studio's UI.

Studio (default http://127.0.0.1:8648) runs Hermes (or a coding agent: claude-code, codex, ...)
in a folder: POST /api/studio/chat-run/runs {input, instructions, workspace, source, coding_agent_id?}
with "Authorization: Bearer <JWT>" (POST /api/auth/login {username, password} returns one). The run
blocks until it ends (up to 30 minutes), so it runs in a background thread here. Messages:
GET /api/studio/sessions/conversations/{id}/messages; changed files:
GET /api/studio/sessions/{id}/workspace-run-changes. Open it: {url}/#/hermes/session/{id}."""
from __future__ import annotations

import os
import threading
import time
import uuid

import requests

from . import config, credentials, store

TIMEOUT = 15
RUN_TIMEOUT_MS = 1_800_000
_RUNS: dict[str, dict] = {}
_LOCK = threading.Lock()


class StudioError(Exception):
    pass


def settings() -> dict:
    s = dict(store.settings().get("studio") or {})
    s.setdefault("enabled", False)
    s.setdefault("url", os.environ.get("HERMES_STUDIO_URL", "").strip() or "http://127.0.0.1:8648")
    s.setdefault("profile", "")
    s.setdefault("agent", "")   # "" = Hermes itself; or claude-code, codex, opencode, pi, grok
    return s


def _headers() -> dict:
    tok = credentials.get("studio_token")
    h = {"Content-Type": "application/json"}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    if settings()["profile"]:
        h["x-hermes-profile"] = settings()["profile"]
    return h


def _req(method: str, path: str, timeout: float = TIMEOUT, **kw) -> dict:
    s = settings()
    if not s["enabled"]:
        raise StudioError("Hermes Studio isn't set up: turn it on in Settings → Hermes Studio")
    try:
        r = requests.request(method, s["url"].rstrip("/") + path, headers=_headers(), timeout=timeout, **kw)
    except requests.RequestException as e:
        raise StudioError(f"can't reach Hermes Studio at {s['url']}: {e}") from e
    if r.status_code == 401:
        raise StudioError("Studio wants a sign-in: save a Studio token (or sign in) under Settings → Hermes Studio")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json().get("message")
        except ValueError:
            msg = r.text[:300]
        raise StudioError(f"Studio said {r.status_code}: {msg}")
    return r.json() if r.content else {}


def login(username: str, password: str) -> None:
    s = settings()
    try:
        r = requests.post(s["url"].rstrip("/") + "/api/auth/login", json={"username": username, "password": password}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise StudioError(f"can't reach Hermes Studio at {s['url']}: {e}") from e
    if r.status_code >= 400 or not (r.json() or {}).get("token"):
        raise StudioError("Studio didn't accept that sign-in")
    credentials.set("studio_token", r.json()["token"])


def status() -> dict:
    s = settings()
    out = {"enabled": s["enabled"], "url": s["url"], "agent": s["agent"], "profile": s["profile"], "token_set": bool(credentials.get("studio_token"))}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off"}
    try:
        _req("GET", "/api/studio/sessions", params={"limit": 1})
        return {**out, "ok": True}
    except StudioError as e:
        return {**out, "ok": False, "error": str(e)}


def link(session_id: str) -> str:
    return settings()["url"].rstrip("/") + f"/#/hermes/session/{session_id}"


def run(message: str, workdir: str, instructions: str, session_id: str | None = None, on_done=None) -> str:
    """Start (or continue) a Studio run in the background; returns its session id at once."""
    sid = session_id or str(uuid.uuid4())
    with _LOCK:
        if _RUNS.get(sid, {}).get("running"):
            raise StudioError("Studio is still working on the last message")
        _RUNS[sid] = {"running": True, "since": config.now_iso(), "error": None}
    s = settings()
    body = {"input": message, "session_id": sid, "workspace": workdir, "instructions": instructions,
            "source": "upstream-manager", "timeout_ms": RUN_TIMEOUT_MS}
    if s["agent"]:
        body["coding_agent_id"] = s["agent"]

    def go():
        err = None
        try:
            _req("POST", "/api/studio/chat-run/runs", timeout=RUN_TIMEOUT_MS / 1000 + 60, json=body)
        except Exception as e:
            err = str(e)
        with _LOCK:
            _RUNS[sid] = {**_RUNS[sid], "running": False, "error": err, "ended": time.time()}
        if on_done:
            try:
                on_done(err)
            except Exception:
                pass

    threading.Thread(target=go, daemon=True, name=f"studio-{sid[:8]}").start()
    return sid


def run_state(session_id: str) -> dict:
    with _LOCK:
        return dict(_RUNS.get(session_id) or {"running": False, "error": None})


def messages(session_id: str) -> list[dict]:
    res = _req("GET", f"/api/studio/sessions/conversations/{session_id}/messages")
    items = res if isinstance(res, list) else res.get("messages") or res.get("data") or res.get("items") or []
    out = []
    for m in items:
        role, content = m.get("role"), m.get("content")
        if isinstance(content, list):
            content = "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
        if role in ("user", "assistant") and (content or "").strip():
            out.append({"role": role, "content": content})
        elif role == "tool" or m.get("tool_calls"):
            names = [((c.get("function") or {}).get("name") or c.get("name") or "a tool") for c in (m.get("tool_calls") or []) if isinstance(c, dict)]
            out.append({"role": "tool", "content": "used " + (", ".join(names) or m.get("tool_name") or "a tool")})
    return out
