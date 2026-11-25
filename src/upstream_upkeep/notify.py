"""Notifications: what needs you, in the app's own feed and on your phone.

Every notification lands in the feed (the bell at the top of the page). It is also sent to the channels
you set up, for the kinds of event you keep on:

  ntfy     UPSTREAM_NTFY_URL (e.g. https://ntfy.sh/my-secret-topic), UPSTREAM_NTFY_TOKEN if the topic is protected
  Gotify   UPSTREAM_GOTIFY_URL and UPSTREAM_GOTIFY_TOKEN (an application token)
  webhook  UPSTREAM_NOTIFY_WEBHOOK: a JSON POST {event, level, title, message, url, repo, at}

or the same on Settings -> Notifications (.env wins). A notification with a `key` is sent once: the same
key again within DEDUPE_HOURS only updates the feed entry, so a request that waits for days doesn't ring
every time the Overseer looks at it.
"""
from __future__ import annotations

import os
import threading
import time
import uuid

import requests

from . import config, credentials, store

EVENTS = {
    "request": "A request waits for your approval",
    "auto_failed": "An auto-approval check failed",
    "auto_approved": "A request went in by itself",
    "agent_waiting": "An agent is asking you something",
    "task_done": "An agent finished a task",
    "overseer": "The Overseer found something urgent",
    "stuck": "Work is stuck (an agent stopped, a claim ran out)",
    "daily": "The daily check had errors",
    "message": "An agent sent you a message",
}
DEFAULT_OFF = {"auto_approved", "task_done"}       # in the feed, not on the phone, unless you turn them on
LEVELS = {"low": (2, 2), "info": (3, 5), "high": (4, 8), "urgent": (5, 10)}   # (ntfy, gotify) priority
DEDUPE_HOURS = 6
FEED_MAX = 300
_sent: dict[str, float] = {}
_lock = threading.Lock()


def settings() -> dict:
    s = dict(store.settings().get("notify") or {})
    ev = dict(s.get("events") or {})
    s["events"] = {k: bool(ev.get(k, k not in DEFAULT_OFF)) for k in EVENTS}
    s["ntfy_url"] = os.environ.get("UPSTREAM_NTFY_URL") or s.get("ntfy_url") or ""
    s["gotify_url"] = os.environ.get("UPSTREAM_GOTIFY_URL") or s.get("gotify_url") or ""
    s["webhook_url"] = os.environ.get("UPSTREAM_NOTIFY_WEBHOOK") or s.get("webhook_url") or ""
    s["public_url"] = (os.environ.get("UPSTREAM_PUBLIC_URL") or s.get("public_url") or "").rstrip("/")
    s["from_env"] = [k for k, v in (("ntfy_url", "UPSTREAM_NTFY_URL"), ("gotify_url", "UPSTREAM_GOTIFY_URL"),
                                    ("webhook_url", "UPSTREAM_NOTIFY_WEBHOOK"), ("public_url", "UPSTREAM_PUBLIC_URL"))
                     if os.environ.get(v)]
    return s


def public() -> dict:
    s = settings()
    return {**{k: s[k] for k in ("ntfy_url", "gotify_url", "webhook_url", "public_url", "events", "from_env")},
            "ntfy_token_set": bool(credentials.get("ntfy_token")), "gotify_token_set": bool(credentials.get("gotify_token")),
            "names": EVENTS, "channels": channels()}


def save(data: dict) -> dict:
    cur = dict(store.settings().get("notify") or {})
    for k in ("ntfy_url", "gotify_url", "webhook_url", "public_url"):
        if k in data:
            v = str(data[k] or "").strip()
            if v and not v.startswith(("http://", "https://")):
                raise ValueError(f"{k.replace('_', ' ')} must start with http:// or https://")
            cur[k] = v
    if isinstance(data.get("events"), dict):
        cur["events"] = {k: bool(data["events"].get(k)) for k in EVENTS}
    for field in ("ntfy_token", "gotify_token"):
        if isinstance(data.get(field), str) and data[field].strip() and not data[field].startswith("••"):
            credentials.set(field, data[field].strip())
    store.save_settings({"notify": cur})
    return public()


def channels() -> list[str]:
    s = settings()
    return [n for n, ok in (("ntfy", s["ntfy_url"]), ("gotify", s["gotify_url"] and credentials.get("gotify_token")),
                            ("webhook", s["webhook_url"])) if ok]


# ------------------------------------------------------------------ the feed
def _feed() -> list[dict]:
    return store._ensure().setdefault("notifications", [])


def feed(limit: int = 100, unread_only: bool = False) -> dict:
    items = [n for n in reversed(_feed()) if not unread_only or not n.get("read")][:limit]
    return {"items": items, "unread": sum(1 for n in _feed() if not n.get("read"))}


@store._locked
def mark_read(ids: list[str] | None = None) -> int:
    n = 0
    for item in _feed():
        if not item.get("read") and (ids is None or item["id"] in ids):
            item["read"], n = True, n + 1
    store.save_state()
    return n


# ------------------------------------------------------------------ sending
def notify(event: str, title: str, message: str = "", level: str = "info", path: str | None = None,
           key: str | None = None, repo: str | None = None, wait: bool = False, force: bool = False) -> dict | None:
    """Record a notification and send it to the channels on for `event`. `path` is a page of this app
    ("/requests", "/work?task=x"): the feed links to it, and phones get the full link when UPSTREAM_PUBLIC_URL is set.
    Returns the feed entry, or None when the same `key` was sent recently."""
    if event not in EVENTS:
        raise ValueError(f"unknown event {event}")
    level = level if level in LEVELS else "info"
    now = time.time()
    with _lock:
        if key and now - _sent.get(key, 0) < DEDUPE_HOURS * 3600:
            return None
        if key:
            _sent[key] = now
    entry = {"id": uuid.uuid4().hex[:10], "at": config.now_iso(), "event": event, "level": level,
             "title": title[:200], "message": (message or "")[:2000], "path": path, "repo": repo, "read": False}
    with store.lock:
        f = _feed()
        f.append(entry)
        del f[:-FEED_MAX]
        store.save_state()
    s = settings()
    if force or s["events"].get(event):
        t = threading.Thread(target=_deliver, args=(entry, s), daemon=True, name="notify")
        t.start()
        if wait:
            t.join(30)
    return entry


def _deliver(entry: dict, s: dict) -> list[dict]:
    url = f"{s['public_url']}{entry['path']}" if s["public_url"] and entry.get("path") else None
    ntfy_p, gotify_p = LEVELS[entry["level"]]
    results = []

    def attempt(name, fn):
        try:
            r = fn()
            results.append({"channel": name, "ok": r.status_code < 400, "status": r.status_code})
        except requests.RequestException as e:
            results.append({"channel": name, "ok": False, "error": str(e)[:200]})

    if s["ntfy_url"]:
        headers = {"Title": entry["title"].encode("utf-8").decode("latin-1", "ignore"), "Priority": str(ntfy_p),
                   "Tags": {"urgent": "rotating_light", "high": "warning"}.get(entry["level"], "arrows_counterclockwise")}
        if url:
            headers["Click"] = url
        if credentials.get("ntfy_token"):
            headers["Authorization"] = f"Bearer {credentials.get('ntfy_token')}"
        attempt("ntfy", lambda: requests.post(s["ntfy_url"], data=(entry["message"] or entry["title"]).encode(), headers=headers, timeout=15))
    if s["gotify_url"] and credentials.get("gotify_token"):
        attempt("gotify", lambda: requests.post(s["gotify_url"].rstrip("/") + "/message", timeout=15,
                                                 headers={"X-Gotify-Key": credentials.get("gotify_token")},
                                                 json={"title": entry["title"], "message": entry["message"] + (f"\n{url}" if url else ""),
                                                       "priority": gotify_p, **({"extras": {"client::notification": {"click": {"url": url}}}} if url else {})}))
    if s["webhook_url"]:
        attempt("webhook", lambda: requests.post(s["webhook_url"], timeout=15, json={**entry, "url": url}))
    entry["sent"] = results
    return results


def test() -> dict:
    entry = notify("overseer", "Upstream Manager test", "Notifications work. You'll get these when something needs you.",
                   level="info", path="/overseer", wait=True, force=True)
    return {"ok": True, "channels": channels(), "sent": (entry or {}).get("sent", [])}
