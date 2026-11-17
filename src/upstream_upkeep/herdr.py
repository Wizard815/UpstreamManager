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
import shutil
import socket
import subprocess
import uuid
from pathlib import Path

from . import store

TIMEOUT = 20
KINDS = ("hermes", "claude", "codex", "opencode", "copilot", "cursor", "pi", "droid", "kimi", "qwen", "grok")


class HerdrError(Exception):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def settings() -> dict:
    """Saved settings win; the env file fills the rest. With nothing saved, herdr is on when its
    socket is there (the same box) or when UPSTREAM_HERDR=on."""
    s = dict(store.settings().get("herdr") or {})
    env = os.environ.get
    s.setdefault("socket", "")
    s.setdefault("session", env("HERDR_SESSION", ""))
    s.setdefault("kind", env("UPSTREAM_HERDR_KIND", "hermes"))   # which agent to start for a task
    s.setdefault("args", env("UPSTREAM_HERDR_ARGS", ""))
    s.setdefault("machine", env("UPSTREAM_HERDR_MACHINE", ""))   # "" = this machine; else a saved herdr machine
    s.setdefault("bin", env("UPSTREAM_HERDR_BIN", ""))
    s.setdefault("path_map", env("UPSTREAM_HERDR_PATH_MAP", ""))  # "/local/prefix=/remote/prefix" for other machines
    if "enabled" not in s:
        flag = env("UPSTREAM_HERDR", "").strip().lower()
        s["enabled"] = flag in ("1", "on", "true", "yes") or (flag not in ("0", "off", "false", "no") and os.path.exists(_socket_for(s)))
        s["auto"] = True
    return s


def _socket_for(s: dict) -> str:
    if (s.get("socket") or "").strip():
        return os.path.expanduser(s["socket"].strip())
    if os.environ.get("HERDR_SOCKET_PATH"):
        return os.environ["HERDR_SOCKET_PATH"]
    base = Path.home() / ".config" / "herdr"
    sess = (s.get("session") or "").strip()
    return str(base / "sessions" / sess / "herdr.sock") if sess else str(base / "herdr.sock")


def socket_path() -> str:
    return _socket_for(settings())


def binary() -> str | None:
    return settings()["bin"].strip() or shutil.which("herdr")


def remote_path(path: str, machine: str | None) -> str:
    """Where a local folder is on another machine: the same path (a shared mount) unless a map says otherwise."""
    if not machine:
        return path
    best = None
    for pair in settings()["path_map"].split(","):
        local, _, remote = pair.partition("=")
        local, remote = local.strip().rstrip("/"), remote.strip().rstrip("/")
        if local and remote and (path == local or path.startswith(local + "/")) and (best is None or len(local) > len(best[0])):
            best = (local, remote)
    return best[1] + path[len(best[0]):] if best else path


_TARGET = re.compile(r"^([a-z][a-z0-9_-]{0,31}|w\d+:p\d+)$")     # a live agent's name, or the pane it is in


def check_target(name) -> str:
    if not isinstance(name, str) or not _TARGET.fullmatch(name):
        raise HerdrError("pick an agent by its herdr name (like reviewer) or its pane (like w1:p1)", "invalid_agent_name")
    return name


def check_machine(machine) -> str | None:
    if machine in (None, ""):
        return None
    if not isinstance(machine, str) or machine.startswith("-") or "\0" in machine or len(machine) > 100:
        raise HerdrError("that isn't one of herdr's saved machines", "invalid_machine")
    return machine


def call(method: str, params: dict | None = None, timeout: float = TIMEOUT, machine: str | None = None) -> dict:
    """One herdr API call: over the local socket, or through herdr's own machine forwarding
    (`herdr --machine <label> ...`, JSON over SSH) when the agent lives on another machine."""
    machine = check_machine(machine)
    if machine:
        return _cli(machine, method, params or {}, timeout)
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


def _argv(method: str, p: dict) -> list[str]:
    """The herdr CLI command for an API method (the CLI is how herdr forwards to a saved machine)."""
    if method in ("ping", "agent.list"):
        return ["agent", "list"]
    if method == "workspace.create":
        return ["workspace", "create", "--cwd", p["cwd"], "--label", p.get("label") or "task", "--no-focus",
                *[x for k, v in (p.get("env") or {}).items() for x in ("--env", f"{k}={v}")]]
    if method == "workspace.close":
        return ["workspace", "close", p["workspace_id"]]
    if method == "agent.start":
        return ["agent", "start", p["name"], "--kind", p["kind"], "--pane", p["pane_id"], "--timeout", str(p.get("timeout_ms", 30000)),
                *(["--", *p["args"]] if p.get("args") else [])]
    if method == "agent.prompt":
        text = p["text"]  # the CLI must never read the task's words as one of its own flags
        return ["agent", "prompt", p["target"], (" " + text) if text.lstrip().startswith("-") else text]
    if method == "agent.get":
        return ["agent", "get", p["target"]]
    if method == "agent.read":
        return ["agent", "read", p["target"], "--source", p.get("source", "recent-unwrapped"), "--lines", str(p.get("lines", 200))]
    if method == "agent.send_keys":
        return ["agent", "send-keys", p["target"], *p["keys"]]
    raise HerdrError(f"herdr method {method} can't be forwarded to another machine")


def _cli(machine: str, method: str, params: dict, timeout: float) -> dict:
    exe = binary()
    if not exe:
        raise HerdrError("to reach herdr on another machine, the herdr command must be installed here too (it does the forwarding)", "no_cli")
    try:
        r = subprocess.run([exe, "--machine", machine, *_argv(method, params)], capture_output=True, text=True, timeout=timeout + 30,
                           stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise HerdrError(f"herdr on {machine} didn't answer in time", "timeout")
    except OSError as e:
        raise HerdrError(f"can't run {exe}: {e}", "no_cli") from e
    if r.returncode:
        try:
            err = (json.loads(r.stderr.strip().splitlines()[-1]) or {}).get("error") or {}
        except (ValueError, IndexError):
            err = {}
        raise HerdrError(f"herdr on {machine}: {err.get('message') or r.stderr.strip()[-300:] or 'failed'}", err.get("code") or "remote_failed")
    if method == "agent.read":  # the CLI prints the text itself
        return {"read": {"text": r.stdout}}
    if method == "ping":
        return {"version": None}
    try:
        return (json.loads(r.stdout) or {}).get("result") or {}
    except ValueError as e:
        raise HerdrError(f"herdr on {machine} sent something that isn't JSON") from e


def machines() -> list[dict]:
    """The other machines this herdr knows (herdr machine add): [{id, label, target, enabled}]."""
    exe = binary()
    if not exe:
        return []
    try:
        r = subprocess.run([exe, "machine", "list", "--json"], capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL)
        rows = json.loads(r.stdout) if r.returncode == 0 else []
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return []
    return [{"id": m.get("id"), "label": m.get("label"), "target": m.get("target"), "enabled": bool(m.get("enabled"))}
            for m in rows if isinstance(m, dict) and m.get("label")]


# The command each agent kind runs, to see which are installed here (as Hermes Studio lists coding agents).
_COMMAND = {"cursor": "cursor-agent"}


def installed_kinds() -> list[str]:
    import shutil
    return [k for k in KINDS if shutil.which(_COMMAND.get(k, k))]


def status() -> dict:
    s = settings()
    here = installed_kinds()
    # installed ones first (the default always first), then the rest herdr knows
    kinds = list(dict.fromkeys([s["kind"], *here, *KINDS]))
    out = {"enabled": s["enabled"], "auto": bool(s.get("auto")), "socket": socket_path(), "kind": s["kind"], "kinds": kinds,
           "installed": here,
           "machine": s["machine"], "path_map": s["path_map"], "cli": bool(binary())}
    if not s["enabled"]:
        return {**out, "ok": False, "error": "off" if not s.get("auto") else f"herdr isn't running here (no socket at {out['socket']})"}
    try:
        r = call("ping", machine=s["machine"] or None, timeout=4)
        where = f"on {s['machine']}" if s["machine"] else "on this machine"
        return {**out, "ok": True, "version": r.get("version"), "where": where, "machines": [m for m in machines() if m["enabled"]]}
    except HerdrError as e:
        return {**out, "ok": False, "error": str(e)}


def _info(a: dict) -> dict:
    sess = a.get("agent_session") or {}
    return {"name": a.get("name"), "kind": a.get("agent") or a.get("display_agent"), "status": a.get("agent_status") or "unknown",
            "pane_id": a.get("pane_id"), "workspace_id": a.get("workspace_id"), "title": a.get("title") or a.get("terminal_title_stripped"),
            "cwd": a.get("foreground_cwd") or a.get("cwd"), "session": sess.get("value"), "session_agent": sess.get("agent")}


def agents(machine: str | None = None) -> list[dict]:
    """Every agent herdr is running now (named or not), with herdr's status for each."""
    return [{**_info(a), "machine": machine or ""} for a in call("agent.list", machine=machine).get("agents") or []]


def all_agents() -> tuple[list[dict], dict]:
    """Agents here and on every machine this herdr is connected to; and what couldn't be reached."""
    s = settings()
    places = list(dict.fromkeys([s["machine"] or "", *[m["label"] for m in machines() if m["enabled"]]]))
    if os.path.exists(socket_path()) and "" not in places:
        places.insert(0, "")
    out, errors = [], {}
    for m in places:
        try:
            out += agents(m or None)
        except HerdrError as e:
            errors[m or "this machine"] = str(e)
    return out, errors


def agent_name(title: str, task_id: str, attempt: int = 1) -> str:
    """A name a person can read in herdr's sidebar, unique per task: fix-the-parser-8f2a
    (and fix-the-parser-8f2a-2 for the task's second agent)."""
    tail = f"-{task_id[:4].lower()}" + (f"-{attempt}" if attempt > 1 else "")
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[: 31 - len(tail)].strip("-") or "task"
    if not slug[0].isalpha():
        slug = ("t-" + slug)[: 31 - len(tail)].strip("-")
    return slug + tail


def start(task_id: str, title: str, workdir: str, prompt: str, kind: str | None = None, env: dict | None = None,
          machine: str | None = None, attempt: int = 1) -> dict:
    """Open a herdr workspace in the task's folder and start an agent there. The task is prompted once
    the agent is ready; if it comes up blocked (a trust dialog, a login), the prompt is kept for later."""
    s = settings()
    if not s["enabled"]:
        raise HerdrError("herdr isn't set up: turn it on in Settings → herdr")
    kind = (kind or s["kind"] or "hermes").strip()
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", kind):
        raise HerdrError("the agent kind is a name like claude, codex or hermes")
    if machine is None:
        machine = s["machine"] or None
    machine = machine or None
    C = lambda m, p=None, **kw: call(m, p, machine=machine, **kw)  # noqa: E731
    ws = C("workspace.create", {"cwd": remote_path(workdir, machine), "label": f"⇅ {title}"[:60], "focus": False, **({"env": env} if env else {})})
    pane = (ws.get("root_pane") or {}).get("pane_id")
    wid = (ws.get("workspace") or {}).get("workspace_id")
    if not pane:
        raise HerdrError("herdr made no pane for the workspace")
    name = agent_name(title, task_id, attempt)
    args = [a for a in s["args"].split() if a] if s["args"] else []
    out = {"agent": name, "kind": kind, "workspace_id": wid, "pane_id": pane, "ours": True, "pending": None, "machine": machine or "",
           "attempt": attempt}
    try:
        C("agent.start", {"name": name, "kind": kind, "pane_id": pane, **({"args": args} if args else {}), "timeout_ms": 120000}, timeout=150)
    except HerdrError as e:
        if e.code != "agent_not_ready":  # anything else: don't leave an empty workspace behind
            try:
                C("workspace.close", {"workspace_id": wid})
            except HerdrError:
                pass
            raise
        return {**out, "pending": prompt}  # it is asking something first: the user answers, then we prompt
    try:
        C("agent.prompt", {"target": name, "text": prompt})
    except HerdrError as e:
        if e.code != "agent_blocked":
            raise
        out["pending"] = prompt
    return out


def prompt(name: str, text: str, machine: str | None = None) -> None:
    check_target(name)
    call("agent.prompt", {"target": name, "text": text}, machine=machine or None)


def agent(name: str, machine: str | None = None) -> dict:
    """{status, output, session...}: what the agent is doing and its recent screen."""
    machine = machine or None
    check_target(name)
    info = _info(call("agent.get", {"target": name}, machine=machine).get("agent") or {})
    try:
        text = (call("agent.read", {"target": name, "source": "recent-unwrapped", "lines": 200}, machine=machine).get("read") or {}).get("text", "")
    except HerdrError:
        text = ""
    return {**info, "output": text[-20000:]}


_KEY = re.compile(r"^(enter|esc|tab|up|down|left|right|backspace|space|ctrl\+[a-z]|[A-Za-z0-9])$")


def send_keys(name: str, keys: list[str], machine: str | None = None) -> None:
    """Answer an agent's dialog (or interrupt it) with logical keys: enter, esc, y, 1, ctrl+c..."""
    if not isinstance(keys, list) or not keys or len(keys) > 20 or not all(isinstance(k, str) and _KEY.fullmatch(k) for k in keys):
        raise HerdrError("keys are a list of names like enter, esc, y, 1 or ctrl+c")
    check_target(name)
    call("agent.send_keys", {"target": name, "keys": keys}, machine=machine or None)


def close_workspace(workspace_id: str, machine: str | None = None) -> None:
    call("workspace.close", {"workspace_id": workspace_id}, machine=machine or None)
