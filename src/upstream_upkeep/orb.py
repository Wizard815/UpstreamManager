"""Pondering Orb: park a task there for later.

Orb (a homelab dashboard) keeps tasks in lists synced with CalDAV. We create one with its REST API,
POST {url}/api/c/tasks {title, list, notes, priority}, with "Authorization: Bearer <token>" when Orb
has ORB_API_TOKENS set (save the token under Accounts as orb_token). Settings: {"orb": {"url", "list"}}."""
from __future__ import annotations

import os

import requests

from . import credentials, store

TIMEOUT = 15


def _why(e: Exception) -> str:
    """A network error in a few words."""
    t = str(e)
    return ("nothing is listening there (is it running?)" if "Connection refused" in t else "timed out" if "timed out" in t.lower()
            else "that name doesn't resolve" if "resolve" in t or "Name or service" in t else type(e).__name__)


class OrbError(Exception):
    pass


def settings() -> dict:
    s = dict(store.settings().get("orb") or {})
    s.setdefault("url", os.environ.get("ORB_URL", "").strip())
    s.setdefault("list", os.environ.get("ORB_TASK_LIST", "").strip() or "Inbox")
    return s


def status() -> dict:
    s = settings()
    out = {"url": s["url"], "list": s["list"], "token_set": bool(credentials.get("orb_token")), "configured": bool(s["url"])}
    if not s["url"]:
        return {**out, "ok": False, "error": "no Pondering Orb address set"}
    try:
        r = requests.get(s["url"].rstrip("/") + "/api/c/tasks", headers=_headers(), timeout=TIMEOUT, params={"limit": 1})
        if r.status_code in (401, 403):
            return {**out, "ok": False, "error": f"Orb refused the token ({r.status_code})"}
        r.raise_for_status()
        return {**out, "ok": True}
    except requests.RequestException as e:
        return {**out, "ok": False, "error": f"can't reach Pondering Orb at {s['url']} ({_why(e)})"}


def _headers() -> dict:
    tok = credentials.get("orb_token")
    return {"Content-Type": "application/json", **({"Authorization": f"Bearer {tok}"} if tok else {})}


def add_task(title: str, notes: str, priority: int = 0, task_list: str | None = None) -> dict:
    """Create the task; returns {id, url}. Orb takes no source field, so the link goes in notes."""
    s = settings()
    if not s["url"]:
        raise OrbError("Pondering Orb isn't set up: add its address in Settings")
    base = s["url"].rstrip("/")
    body = {"title": title[:200], "list": (task_list or s["list"] or "Inbox"), "notes": notes[:8000],
            "priority": max(0, min(9, int(priority)))}
    try:
        r = requests.post(base + "/api/c/tasks", json=body, headers=_headers(), timeout=TIMEOUT)
    except requests.RequestException as e:
        raise OrbError(f"can't reach Pondering Orb at {base} ({_why(e)})") from e
    if r.status_code in (401, 403):
        raise OrbError("Pondering Orb refused the request: save an Orb API token under Accounts")
    if r.status_code >= 400:
        raise OrbError(f"Pondering Orb said {r.status_code}: {r.text[:300]}")
    data = r.json() if r.content else {}
    rec = data.get("item") or data.get("record") or data
    tid = str(rec.get("id") or "")
    return {"id": tid, "list": body["list"], "url": base + "/#todo"}
