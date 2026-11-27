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


def _why(e: Exception) -> str:
    """A network error in a few words."""
    t = str(e)
    return ("nothing is listening there (is it running?)" if "Connection refused" in t else "timed out" if "timed out" in t.lower()
            else "that name doesn't resolve" if "resolve" in t or "Name or service" in t else type(e).__name__)


class StudioError(Exception):
    pass


# What Studio told us about itself the last time its Upstream tab forwarded a request here: its own
# address and a login for its administrator. Opening Upstream inside Studio is then all the setup
# Upstream needs to call Studio back (runs, herdr agents). Kept in memory and in the credentials file.
_FROM_STUDIO: dict = {}


def adopt(url: str, token: str) -> bool:
    """Called for requests Studio's proxy forwarded (see app._via_trusted_proxy). Returns True when new."""
    url = (url or "").strip().rstrip("/")
    token = (token or "").strip()
    if not url.startswith(("http://", "https://")) or not token or len(token) > 4000:
        return False
    if _FROM_STUDIO.get("url") == url and _FROM_STUDIO.get("token") == token:
        return False
    _FROM_STUDIO.update(url=url, token=token, at=time.time())
    credentials.set("studio_proxy_token", token)
    if (store.settings().get("studio") or {}).get("from_studio_url") != url:
        store.save_settings({"studio": {**(store.settings().get("studio") or {}), "from_studio_url": url}})
    _HERDR.clear()
    return True


def settings() -> dict:
    s = dict(store.settings().get("studio") or {})
    seen = s.pop("from_studio_url", None)
    env_url = os.environ.get("HERMES_STUDIO_URL", "").strip()
    # an address in the env file turns it on; so does Studio having opened Upstream in its tab
    s.setdefault("enabled", bool(env_url or seen))
    s.setdefault("url", env_url or seen or "http://127.0.0.1:8648")
    s["herdr"] = os.environ.get("UPSTREAM_HERDR_VIA_STUDIO", "").strip().lower() or s.get("herdr") or "auto"
    s.setdefault("profile", os.environ.get("HERMES_STUDIO_PROFILE", "").strip())
    s.setdefault("agent", os.environ.get("HERMES_STUDIO_AGENT", "").strip())   # "" = Hermes itself; or claude-code, codex, opencode, pi, grok
    return s


def _token() -> str:
    """The saved or env token; else sign in once with HERMES_STUDIO_USER / HERMES_STUDIO_PASSWORD from the env file."""
    tok = credentials.get("studio_token")
    user, pw = os.environ.get("HERMES_STUDIO_USER", "").strip(), os.environ.get("HERMES_STUDIO_PASSWORD", "")
    if not tok and user and pw and not _LOGIN.get("tried"):
        _LOGIN["tried"] = True
        try:
            login(user, pw)
            tok = credentials.get("studio_token")
        except StudioError:
            pass
    # last: the login Studio handed over when Upstream was opened in its tab
    return tok or _FROM_STUDIO.get("token") or credentials.get("studio_proxy_token")


_LOGIN: dict = {}


def _headers() -> dict:
    tok = _token()
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
        raise StudioError(f"can't reach Hermes Studio at {s['url']} ({_why(e)})") from e
    if r.status_code == 401:
        raise StudioError("Studio wants a sign-in: open Upstream once in Studio's Upstream tab (that connects them), "
                          "or set HERMES_STUDIO_USER / HERMES_STUDIO_PASSWORD")
    if r.status_code >= 400:
        try:
            msg = r.json().get("error") or r.json().get("message")
        except ValueError:
            msg = r.text[:300]
        raise StudioError(f"Studio said {r.status_code}: {msg}")
    try:
        return r.json() if r.content else {}
    except ValueError:
        raise StudioError(f"Studio sent something that isn't JSON (is {s['url']} really Hermes Studio?)")


def login(username: str, password: str) -> None:
    s = settings()
    try:
        r = requests.post(s["url"].rstrip("/") + "/api/auth/login", json={"username": username, "password": password}, timeout=TIMEOUT)
    except requests.RequestException as e:
        raise StudioError(f"can't reach Hermes Studio at {s['url']} ({_why(e)})") from e
    try:
        token = (r.json() or {}).get("token") if r.status_code < 400 else None
    except ValueError:
        token = None
    if not token:
        raise StudioError("Studio didn't accept that sign-in")
    credentials.set("studio_token", token)


def status() -> dict:
    s = settings()
    out = {"enabled": s["enabled"], "url": s["url"], "agent": s["agent"], "profile": s["profile"], "token_set": bool(_token()),
           "from_studio": bool(_FROM_STUDIO.get("token") or credentials.get("studio_proxy_token")), "herdr_via_studio": s["herdr"]}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off"}
    try:
        _req("GET", "/api/studio/sessions", timeout=2.5, params={"limit": 1})
        return {**out, "ok": True}
    except StudioError as e:
        return {**out, "ok": False, "error": str(e)}


def link(session_id: str) -> str:
    return settings()["url"].rstrip("/") + f"/#/hermes/session/{session_id}"


# ------------------------------------------------------------------ herdr through Studio
# On the machine Studio runs on, herdr agents start through Studio's own herdr bridge: each one is then
# a Studio chat in its herdr space (Studio's models, profile and folders), and Studio tells Upstream
# when the agent starts working, waits for an answer or finishes. Other machines: Upstream's own herdr.
# Upstream's kinds → Studio's coding agent ids (only claude differs).
STUDIO_AGENT = {"claude": "claude-code"}
_HERDR: dict = {}


def herdr_state(fresh: bool = False) -> dict:
    """Studio's herdr: {connected, panes} (cached for 20 s)."""
    if not fresh and _HERDR.get("at", 0) > time.time() - 20:
        return _HERDR["state"]
    try:
        r = _req("GET", "/api/coding-agents/herdr", timeout=3)
        b = r.get("bridge") or {}
        state = {"connected": bool(b.get("connected")), "panes": b.get("panes") or [], "error": b.get("error") or r.get("error") or None}
    except StudioError as e:
        state = {"connected": False, "panes": [], "error": str(e)}
    _HERDR.update(at=time.time(), state=state)
    return state


def herdr_via_studio() -> bool:
    """Start this machine's herdr agents through Studio? on / off / auto (when Studio's herdr is connected)."""
    s = settings()
    if s["herdr"] == "off" or not s["enabled"] or not _token():
        return False
    return s["herdr"] == "on" or herdr_state()["connected"]


def herdr_launch(kind: str, workdir: str, title: str, prompt: str, task_id: str, repo: str) -> dict:
    """Start an agent in herdr as a Studio chat. Studio sends the prompt once the agent is ready."""
    agent_id = STUDIO_AGENT.get(kind, kind)
    r = _req("POST", "/api/coding-agents/herdr/agents", timeout=60, json={
        "agentId": agent_id, "workspace": workdir, "title": title[:120], "prompt": prompt[:20000],
        "upstream": {"task": task_id, "title": title[:300], "repo": repo[:120]}})
    link_ = r.get("link") or {}
    if not link_.get("paneId"):
        raise StudioError("Studio didn't say which herdr pane the agent got")
    _HERDR.clear()
    return {"pane_id": link_["paneId"], "workspace_id": link_.get("workspaceId")}


def session_for_pane(pane_id: str) -> str | None:
    """The Studio chat an agent's herdr pane belongs to (Studio makes one for each agent it starts)."""
    for fresh in (False, True):
        for p in herdr_state(fresh)["panes"]:
            if p.get("pane_id") == pane_id and p.get("studioSessionId"):
                return p["studioSessionId"]
    return None


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


# ------------------------------------------------------------------ sandboxes
# Throwaway Linux containers on Studio's machine (Studio's docs/sandbox.md): any distro preset or image,
# internet or no network, removed when their time is up. Upstream uses them to try a repo somewhere clean.
SANDBOX_ACTIONS = ("list", "create", "exec", "write_file", "read_file", "copy_repo", "extend", "remove")


def sandbox(action: str, fields: dict | None = None) -> dict:
    """One sandbox action through Studio (POST /api/studio/sandbox/tool/{action}); Studio checks the fields."""
    if action not in SANDBOX_ACTIONS:
        raise StudioError(f"unknown sandbox action {action!r}: {', '.join(SANDBOX_ACTIONS)}")
    fields = dict(fields or {})
    try:
        exec_wait = min(3600, max(1, int(fields.get("timeoutSeconds") or 300))) + 60
    except (TypeError, ValueError):
        raise StudioError("timeoutSeconds is a number of seconds") from None
    wait = exec_wait if action == "exec" else 600 if action in ("create", "copy_repo") else TIMEOUT
    return _req("POST", f"/api/studio/sandbox/tool/{action}", timeout=wait, json=fields)


def try_in_sandbox(repo_url: str, command: str, distro: str = "ubuntu", image: str = "", ref: str = "", network: str = "internet",
                   minutes: int = 30, timeout_seconds: int = 900, keep: bool = False, purpose: str = "") -> dict:
    """Make a sandbox, copy the repo in (as pushed: Studio clones it), run the command in it, and remove the
    sandbox unless keep. Returns the exit code and the end of the output."""
    if not (command or "").strip():
        raise StudioError("give the command to run, e.g. make test")
    made = sandbox("create", {"distro": "" if image else distro, "image": image, "network": network, "minutes": minutes,
                              "purpose": purpose or f"Upstream Manager: {command[:120]}"})
    box = (made.get("sandbox") or {}).get("id")
    if not box:
        raise StudioError("Studio didn't say which sandbox it made")
    try:
        copied = sandbox("copy_repo", {"sandbox": box, "repo": repo_url, "ref": ref})
        ran = sandbox("exec", {"sandbox": box, "command": command, "workdir": copied.get("path") or "/work", "timeoutSeconds": timeout_seconds})
    finally:
        if not keep:
            try:
                sandbox("remove", {"sandbox": box})
            except StudioError:
                pass
    return {"ok": ran.get("exitCode") == 0, "sandbox": box, "kept": keep, "image": (made.get("sandbox") or {}).get("image"),
            "path": copied.get("path"), "exit_code": ran.get("exitCode"), "timed_out": bool(ran.get("timedOut")),
            "output": ran.get("output") or "", "truncated": bool(ran.get("truncated"))}
