"""herdr: hand a task to a coding agent running in herdr, the terminal runtime for agents.

herdr has no network API: it is driven over its local socket (newline-delimited JSON, owner-only),
so Upstream Manager must run as the same user on the same machine as the herdr server. (herdr
reaches other machines itself, with `herdr --machine` over SSH.)

Upstream Manager follows herdr's own rules for agents (its skill, skills/herdr/SKILL.md):
- a task gets a herdr workspace in its branch folder (workspace.create {cwd, label, focus: false}) and an
  agent with a readable unique name ([a-z][a-z0-9_-]{0,31}) started in its root pane (agent.start);
- the task is prompted only when the agent is ready: if it comes up blocked (a trust or login dialog),
  or is blocked later, the prompt is held and sent once it is idle (agent.prompt refuses a blocked agent);
- a blocked agent is asking something: we show its screen and the user answers (agent.send_keys); we
  never answer for them;
- a task can also go to an agent that is already running (herdr to herdr: any live agent by name);
- we only ever close workspaces we created;
- status is herdr's: idle | working | blocked | done | unknown (agent.get / agent.list), output is
  agent.read, and agent_session names the agent's own session (for Hermes: the session Studio shows).
Request:  {"id": "...", "method": "...", "params": {...}}
Response: {"id": "...", "result": {"type": "...", ...}} or {"id": "...", "error": {"code", "message"}}"""
from __future__ import annotations

import json
import os
import re
import socket
import uuid
from pathlib import Path

from . import store

TIMEOUT = 20
KINDS = ("claude", "codex", "hermes", "opencode", "copilot", "cursor", "pi", "droid", "kimi", "qwen", "grok")


class HerdrError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def settings() -> dict:
    s = dict(store.settings().get("herdr") or {})
    s.setdefault("enabled", False)
    s.setdefault("socket", "")
    s.setdefault("session", "")
    s.setdefault("kind", "claude")   # which agent to start for a task
    s.setdefault("args", "")
    return s


def socket_path() -> str:
    s = settings()
    if s["socket"].strip():
        return os.path.expanduser(s["socket"].strip())
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    base = Path.home() / ".config" / "herdr"
    sess = s["session"].strip() or os.environ.get("HERDR_SESSION", "")
    return str(base / "sessions" / sess / "herdr.sock") if sess else str(base / "herdr.sock")


def call(method: str, params: dict | None = None, timeout: float = TIMEOUT) -> dict:
    path = socket_path()
    if not os.path.exists(path):
        raise HerdrError(f"herdr isn't running here (no socket at {path}); start herdr as this user, or set its socket in Settings", "not_running")
    req = {"id": f"um_{uuid.uuid4().hex[:8]}", "method": method, "params": params or {}}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(path)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        raise HerdrError(f"can't talk to herdr at {path}: {e}", "not_running") from e
    try:
        resp = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    except ValueError as e:
        raise HerdrError("herdr sent something that isn't JSON") from e
    if resp.get("error"):
        err = resp["error"]
        raise HerdrError(f"herdr: {err.get('message') or err.get('code')}", err.get("code"))
    return resp.get("result") or {}


def status() -> dict:
    s = settings()
    out = {"enabled": s["enabled"], "socket": socket_path(), "kind": s["kind"], "kinds": list(KINDS)}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off"}
    try:
        r = call("ping")
        return {**out, "ok": True, "version": r.get("version")}
    except HerdrError as e:
        return {**out, "ok": False, "error": str(e)}


def _info(a: dict) -> dict:
    sess = a.get("agent_session") or {}
    return {"name": a.get("name"), "kind": a.get("agent") or a.get("display_agent"), "status": a.get("agent_status") or "unknown",
            "pane_id": a.get("pane_id"), "workspace_id": a.get("workspace_id"), "title": a.get("title") or a.get("terminal_title_stripped"),
            "cwd": a.get("foreground_cwd") or a.get("cwd"), "session": sess.get("value"), "session_agent": sess.get("agent")}


def agents() -> list[dict]:
    """Every agent herdr is running now (named or not), with herdr's status for each."""
    return [_info(a) for a in call("agent.list").get("agents") or []]


def agent_name(title: str, task_id: str) -> str:
    """A name a person can read in herdr's sidebar, unique per task: fix-the-parser-8f2a."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:26].strip("-") or "task"
    if not slug[0].isalpha():
        slug = "t-" + slug[:24]
    return f"{slug}-{task_id[:4].lower()}"[:32]


def start(task_id: str, title: str, workdir: str, prompt: str, kind: str | None = None, env: dict | None = None) -> dict:
    """Open a herdr workspace in the task's folder and start an agent there. The task is prompted once
    the agent is ready; if it comes up blocked (a trust dialog, a login), the prompt is kept for later."""
    s = settings()
    if not s["enabled"]:
        raise HerdrError("herdr isn't set up: turn it on in Settings → herdr")
    kind = (kind or s["kind"] or "claude").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kind):
        raise HerdrError("the agent kind is a name like claude, codex or hermes")
    ws = call("workspace.create", {"cwd": workdir, "label": f"⇅ {title}"[:60], "focus": False, **({"env": env} if env else {})})
    pane = (ws.get("root_pane") or {}).get("pane_id")
    wid = (ws.get("workspace") or {}).get("workspace_id")
    if not pane:
        raise HerdrError("herdr made no pane for the workspace")
    name = agent_name(title, task_id)
    args = [a for a in s["args"].split() if a] if s["args"] else []
    out = {"agent": name, "kind": kind, "workspace_id": wid, "pane_id": pane, "ours": True, "pending": None}
    try:
        call("agent.start", {"name": name, "kind": kind, "pane_id": pane, **({"args": args} if args else {}), "timeout_ms": 120000}, timeout=150)
    except HerdrError as e:
        if e.code != "agent_not_ready":  # anything else: don't leave an empty workspace behind
            try:
                call("workspace.close", {"workspace_id": wid})
            except HerdrError:
                pass
            raise
        return {**out, "pending": prompt}  # it is asking something first: the user answers, then we prompt
    try:
        call("agent.prompt", {"target": name, "text": prompt})
    except HerdrError as e:
        if e.code != "agent_blocked":
            raise
        out["pending"] = prompt
    return out


def prompt(name: str, text: str) -> None:
    call("agent.prompt", {"target": name, "text": text})


def agent(name: str) -> dict:
    """{status, output, session...}: what the agent is doing and its recent screen."""
    info = _info(call("agent.get", {"target": name}).get("agent") or {})
    try:
        text = (call("agent.read", {"target": name, "source": "recent-unwrapped", "lines": 200}).get("read") or {}).get("text", "")
    except HerdrError:
        text = ""
    return {**info, "output": text[-20000:]}


_KEY = re.compile(r"^(enter|esc|tab|up|down|left|right|backspace|space|ctrl\+[a-z]|[A-Za-z0-9])$")


def send_keys(name: str, keys: list[str]) -> None:
    """Answer an agent's dialog (or interrupt it) with logical keys: enter, esc, y, 1, ctrl+c..."""
    if not keys or len(keys) > 20 or not all(isinstance(k, str) and _KEY.fullmatch(k) for k in keys):
        raise HerdrError("keys are names like enter, esc, y, 1 or ctrl+c")
    call("agent.send_keys", {"target": name, "keys": keys})


def close_workspace(workspace_id: str) -> None:
    call("workspace.close", {"workspace_id": workspace_id})
