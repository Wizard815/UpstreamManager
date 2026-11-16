"""herdr: hand a task to a coding agent running in herdr, the terminal runtime for agents.

herdr has no network API: it is driven over its local socket (newline-delimited JSON, owner-only),
so Upstream Manager must run as the same user on the same machine as the herdr server. (herdr
reaches other machines itself, with `herdr --machine` over SSH.)

Per task: workspace.create {cwd: the task's branch folder, label} -> root pane;
agent.start {name, kind, pane_id} (claude, codex, hermes, opencode, ...); agent.prompt {target, text}.
Progress: agent.get (idle|working|blocked|done|unknown) and agent.read (recent output).
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
    pass


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
        raise HerdrError(f"herdr isn't running here (no socket at {path}); start herdr as this user, or set its socket in Settings")
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
        raise HerdrError(f"can't talk to herdr at {path}: {e}") from e
    try:
        resp = json.loads(buf.split(b"\n", 1)[0] or b"{}")
    except ValueError as e:
        raise HerdrError("herdr sent something that isn't JSON") from e
    if resp.get("error"):
        err = resp["error"]
        raise HerdrError(f"herdr: {err.get('message') or err.get('code')}")
    return resp.get("result") or {}


def status() -> dict:
    s = settings()
    out = {"enabled": s["enabled"], "socket": socket_path(), "kind": s["kind"]}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off"}
    try:
        r = call("ping")
        return {**out, "ok": True, "version": r.get("version")}
    except HerdrError as e:
        return {**out, "ok": False, "error": str(e)}


def agent_name(task_id: str) -> str:
    return re.sub(r"[^a-z0-9_-]", "", ("um-" + task_id.lower()))[:32]


def start(task_id: str, title: str, workdir: str, prompt: str, kind: str | None = None) -> dict:
    """Open a herdr workspace in the task's folder, start an agent there and give it the task."""
    s = settings()
    if not s["enabled"]:
        raise HerdrError("herdr isn't set up: turn it on in Settings → herdr")
    kind = (kind or s["kind"] or "claude").strip()
    ws = call("workspace.create", {"cwd": workdir, "label": f"⇅ {title}"[:60], "focus": False})
    pane = (ws.get("root_pane") or {}).get("pane_id")
    if not pane:
        raise HerdrError("herdr made no pane for the workspace")
    name = agent_name(task_id)
    args = [a for a in s["args"].split() if a] if s["args"] else []
    call("agent.start", {"name": name, "kind": kind, "pane_id": pane, **({"args": args} if args else {}), "timeout_ms": 120000}, timeout=150)
    call("agent.prompt", {"target": name, "text": prompt})
    return {"agent": name, "kind": kind, "workspace_id": (ws.get("workspace") or {}).get("workspace_id"), "pane_id": pane}


def prompt(name: str, text: str) -> None:
    call("agent.prompt", {"target": name, "text": text})


def agent(name: str) -> dict:
    """{status, output}: what the agent is doing and its recent screen."""
    info = call("agent.get", {"target": name}).get("agent") or {}
    try:
        text = (call("agent.read", {"target": name, "source": "recent-unwrapped", "lines": 200}).get("read") or {}).get("text", "")
    except HerdrError:
        text = ""
    return {"status": info.get("agent_status") or "unknown", "cwd": info.get("foreground_cwd") or info.get("cwd"), "output": text[-20000:]}


def stop(name: str) -> None:
    call("agent.send_keys", {"target": name, "keys": ["ctrl+c"]})
