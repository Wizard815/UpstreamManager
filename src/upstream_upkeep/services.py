"""Integrations: what Upstream Manager is connected to, and whether each one works right now.

Three things come together here:

- the connections built into the app (herdr, Hermes Studio, the Hermes gateway, Pondering Orb,
  the LLM wiki), with their live status;
- services you add yourself, usually other containers on the box (an address, a health path,
  optionally the repo they are built from), checked the same way;
- a mark on each of your repos saying how far it is integrated (integrated, partly, planned, not
  needed), so you and the agents know what to integrate to and what already works.

Nothing here is written anywhere else (not to the wiki, not to GitHub): it is this app's own record."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse

import requests

from . import config, store

MARKS = {"integrated": "Integrated", "partial": "Partly integrated", "planned": "To integrate", "none": "Not needed"}
TIMEOUT = 2.5

# The connections the app has built in, and the repos they are usually built from (matched by name).
BUILTIN = [
    {"id": "herdr", "name": "herdr", "repos": ["herdr"], "does": "starts coding agents for tasks, here or on other machines"},
    {"id": "studio", "name": "Hermes Studio", "repos": ["hermes-studio"], "does": "tasks become Studio runs; Studio shows this app in its Upstream tab"},
    {"id": "hermes", "name": "Hermes gateway", "repos": ["hermes-agent", "hermes"], "does": "tasks become Hermes sessions"},
    {"id": "orb", "name": "Pondering Orb", "repos": ["pondering-orb", "ponderingorb"], "does": "ideas are parked as to-dos"},
    {"id": "wiki", "name": "LLM wiki", "repos": ["llmwiki", "llm-wiki"], "does": "daily notes, parked ideas, wiki context in chats"},
]


class ServiceError(ValueError):
    pass


def _builtin_status(key: str) -> dict:
    from . import herdr, hermes, orb, studio, wiki
    fn = {"herdr": herdr.status, "studio": studio.status, "hermes": hermes.status, "orb": orb.status, "wiki": wiki.status}[key]
    try:
        s = fn()
    except Exception as e:  # a connector must never break the page
        return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:120]}"}
    addr = s.get("url") or s.get("rest_url") or s.get("socket") or ""
    if key == "herdr" and s.get("machine"):
        addr = f"herdr on {s['machine']}"
    off = s.get("enabled") is False and key in ("hermes", "studio") or (key == "orb" and not s.get("configured"))
    return {"ok": bool(s.get("ok")), "error": None if s.get("ok") else ("off" if off else s.get("error") or "not set up"), "address": addr}


# ------------------------------------------------------------------ your own services (other containers)
def custom() -> list[dict]:
    return list((store.settings().get("services") or {}).get("items") or [])


def _save(items: list[dict]) -> None:
    store.save_settings({"services": {**(store.settings().get("services") or {}), "items": items}})


def _clean(d: dict) -> dict:
    name = str(d.get("name") or "").strip()[:80]
    if not name:
        raise ServiceError("give the service a name")
    url = str(d.get("url") or "").strip()[:300]
    if not re.match(r"^(https?|tcp)://[^/\s]+", url):
        raise ServiceError("the address starts with http://, https:// or tcp:// (e.g. http://127.0.0.1:8095 or tcp://127.0.0.1:5432)")
    health = str(d.get("health") or "").strip()[:200]
    if health and not health.startswith("/"):
        raise ServiceError("the health path starts with / (e.g. /health)")
    repo = str(d.get("repo") or "").strip()
    if repo and not store.get_repo(repo):
        raise ServiceError("that repo isn't tracked here")
    return {"name": name, "url": url, "health": health, "repo": repo or None,
            "container": str(d.get("container") or "").strip()[:120] or None, "notes": str(d.get("notes") or "").strip()[:1000]}


def save(d: dict) -> dict:
    """Add a service, or change one ({id, ...})."""
    items = custom()
    clean = _clean(d)
    if d.get("id"):
        for i, it in enumerate(items):
            if it["id"] == d["id"]:
                items[i] = {**it, **clean}
                _save(items)
                return items[i]
        raise ServiceError("no such service")
    if any(it["name"].casefold() == clean["name"].casefold() for it in items):
        raise ServiceError(f"there is already a service called {clean['name']}")
    item = {"id": uuid.uuid4().hex[:10], "added": config.now_iso(), **clean}
    _save(items + [item])
    return item


def remove(service_id: str) -> None:
    items = custom()
    if not any(it["id"] == service_id for it in items):
        raise ServiceError("no such service")
    _save([it for it in items if it["id"] != service_id])


def check(item: dict) -> dict:
    """Does it answer? HTTP: the health path (or /) gives any status below 500. tcp://: the port takes a connection."""
    url = item["url"]
    t0 = time.monotonic()
    try:
        if url.startswith("tcp://"):
            u = urlparse(url)
            with socket.create_connection((u.hostname, u.port or 80), timeout=TIMEOUT):
                pass
            return {"ok": True, "ms": int((time.monotonic() - t0) * 1000)}
        r = requests.get(url.rstrip("/") + (item.get("health") or "/"), timeout=TIMEOUT, allow_redirects=False)
        ms = int((time.monotonic() - t0) * 1000)
        if r.status_code >= 500:
            return {"ok": False, "error": f"answers, but with an error ({r.status_code})", "ms": ms}
        if item.get("health") and r.status_code >= 400:
            return {"ok": False, "error": f"the health path says {r.status_code}", "ms": ms}
        return {"ok": True, "code": r.status_code, "ms": ms}
    except Exception as e:
        t = str(e)
        why = ("nothing is listening there (is it running?)" if "refused" in t else "timed out" if "timed out" in t.lower()
               else "that name doesn't resolve" if "resolve" in t or "Name or service" in t else type(e).__name__)
        return {"ok": False, "error": why}


# ------------------------------------------------------------------ containers running on this box
def containers() -> dict:
    """Running Docker containers with their published ports, to add one in a click. Needs the docker command."""
    if not shutil.which("docker"):
        return {"available": False, "why": "the docker command isn't installed where this app runs", "items": []}
    try:
        r = subprocess.run(["docker", "ps", "--format", "{{json .}}"], capture_output=True, text=True, timeout=8)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"available": False, "why": str(e)[:200], "items": []}
    if r.returncode:
        return {"available": False, "why": (r.stderr.strip() or "docker ps failed")[:200], "items": []}
    have = {it.get("container") for it in custom()} | {it["url"] for it in custom()}
    out = []
    for line in r.stdout.splitlines():
        try:
            c = json.loads(line)
        except ValueError:
            continue
        ports = sorted({int(p) for p in re.findall(r":(\d+)->\d+/tcp", c.get("Ports", ""))})
        name = c.get("Names", "").split(",")[0]
        out.append({"name": name, "image": c.get("Image"), "status": c.get("Status"), "ports": ports,
                    "url": f"http://127.0.0.1:{ports[0]}" if ports else None, "added": name in have,
                    "repo": _guess_repo(name, c.get("Image") or "")})
    return {"available": True, "items": out}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _guess_repo(*names: str) -> str | None:
    """The tracked repo a container or service is probably built from (its name inside the container's)."""
    keys = [_norm(re.sub(r"[:@].*$", "", n.split("/")[-1])) for n in names if n]
    best = None
    for r in store.all_repos():
        rn = _norm(r.get("name") or r["id"])
        if len(rn) >= 3 and any(rn == k or rn in k for k in keys):
            if not best or len(rn) > len(_norm(best.get("name") or best["id"])):
                best = r
    return best["id"] if best else None


# ------------------------------------------------------------------ marks on repos
def mark(repo_id: str, state: str, note: str | None = None) -> dict:
    repo = store.get_repo(repo_id)
    if not repo:
        raise ServiceError("no such repo")
    if state not in MARKS and state != "":
        raise ServiceError("mark it integrated, partial, planned or none")
    if state:
        repo["integration"] = {"state": state, "note": (note if note is not None else (repo.get("integration") or {}).get("note", "")).strip()[:1000],
                               "at": config.now_iso()}
    else:
        repo.pop("integration", None)
    store.save_repo(repo)
    return repo.get("integration") or {}


# ------------------------------------------------------------------ the whole picture
def overview(live: bool = True) -> dict:
    """Every connection and service with whether it works, and every repo with its mark and what it powers."""
    repos = store.all_repos()
    by_name = {_norm(r.get("name") or r["id"]): r for r in repos}
    rows = []
    for b in BUILTIN:
        repo = next((by_name[_norm(n)] for n in b["repos"] if _norm(n) in by_name), None)
        rows.append({"id": b["id"], "kind": "builtin", "name": b["name"], "does": b["does"], "repo": repo["id"] if repo else None,
                     "repo_name": repo.get("name") if repo else b["repos"][0]})
    for it in custom():
        repo = store.get_repo(it["repo"]) if it.get("repo") else None
        rows.append({**it, "kind": "service", "repo_name": repo.get("name") if repo else None})
    if live:
        with ThreadPoolExecutor(max_workers=8) as pool:
            states = list(pool.map(lambda r: _builtin_status(r["id"]) if r["kind"] == "builtin" else check(r), rows))
        for r, s in zip(rows, states):
            r["status"] = s
            if r["kind"] == "builtin":
                r["address"] = s.pop("address", "")
    powered: dict[str, list[dict]] = {}
    for r in rows:
        st = r.get("status") or {}
        set_up = r["kind"] == "service" or not live or st.get("ok") or st.get("error") not in ("off", "not set up")
        if r.get("repo"):
            powered.setdefault(r["repo"], []).append({"name": r["name"], "ok": st.get("ok"), "set_up": bool(set_up)})
    repo_rows = []
    for r in repos:
        m = r.get("integration") or {}
        runs = powered.get(r["id"], [])
        active = [x for x in runs if x["set_up"]]          # a connection switched off doesn't count
        state = m.get("state") or ("integrated" if active else "")
        works = None if not active or not live else all(x["ok"] for x in active)
        repo_rows.append({"id": r["id"], "name": r.get("name") or r["id"], "state": state, "auto": bool(active and not m.get("state")),
                          "note": m.get("note", ""), "services": runs, "works": works})
    order = {"integrated": 0, "partial": 1, "planned": 2, "": 3, "none": 4}
    repo_rows.sort(key=lambda x: (order.get(x["state"], 3), x["name"].lower()))
    return {"services": rows, "repos": repo_rows, "marks": MARKS}


def for_agents(live: bool = True) -> str:
    """A few lines for a model: what is integrated, what works (when checked live), what is planned."""
    o = overview(live=live)
    lines = ["Integrations (this app's own record):"]
    for s in o["services"]:
        st = s.get("status") or {}
        state = ("works" if st.get("ok") else "not working: " + str(st.get("error"))) if live else "connected"
        lines.append(f"- {s['name']}{' (' + s['repo_name'] + ')' if s.get('repo_name') else ''}: {state}")
    for label, key in (("Partly integrated", "partial"), ("To integrate", "planned")):
        names = [r["name"] + (f" ({r['note']})" if r["note"] else "") for r in o["repos"] if r["state"] == key]
        if names:
            lines.append(f"{label}: " + "; ".join(names))
    return "\n".join(lines)
