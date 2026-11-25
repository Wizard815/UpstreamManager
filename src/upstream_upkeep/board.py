"""The board: tasks as a kanban, and how agents share the work and talk to each other.

Columns are the task statuses (open, working, parked, done). Within a column the Overseer keeps the
order (rank: most important first); a card the user drags keeps the place it was dropped at.

Agents coordinate through it:
- claim:     an agent takes a task (by its name). A claim is a lease: it runs out after LEASE_MINUTES unless
             the agent checks in (heartbeat, a comment, a progress note). One claim per task; another agent
             sees who has it and picks something else.
- comments:  a thread on each task: handoffs, questions, findings. `to` names another agent (or "user").
- messages:  an inbox per agent, for anything not about one task; "*" is everyone, "user" is you (it rings).
Nothing here touches a repo.
"""
from __future__ import annotations

import datetime as dt
import re
import uuid

from . import config, store, tasks

LEASE_MINUTES = 60
COLUMNS = ("open", "working", "parked", "done")
MESSAGES_MAX = 500
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,63}$")


class BoardError(ValueError):
    pass


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _iso(d: dt.datetime) -> str:
    return d.isoformat(timespec="seconds")


def _parse(s: str | None) -> dt.datetime | None:
    try:
        return dt.datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def agent_name(name) -> str:
    name = (name or "").strip() if isinstance(name, str) else ""
    if not _NAME.match(name) or name.lower() in ("user", "you", "*", "overseer"):
        raise BoardError("give your agent name (letters, digits, - _ . : @ /), e.g. claude-1 or herdr:w1:p2")
    return name


def claim_alive(t: dict, now: dt.datetime | None = None) -> bool:
    c = t.get("claimed_by") or {}
    until = _parse(c.get("until"))
    return bool(c.get("agent") and until and until > (now or _now()))


def _seen(agent: str, task_id: str | None = None) -> None:
    agents = store._ensure().setdefault("agents", {})
    a = agents.setdefault(agent, {"first_seen": config.now_iso()})
    a["last_seen"] = config.now_iso()
    if task_id is not None:
        a["task"] = task_id or None


# ------------------------------------------------------------------ the board
def board(repo: str | None = None, group: str | None = None, done_limit: int = 30) -> dict:
    members = None
    if group:
        members = {r["id"] for r in store.all_repos() if group in (r.get("groups") or [])}
    now = _now()
    cols: dict[str, list] = {c: [] for c in COLUMNS}
    for t in tasks.list_tasks():
        if repo and t["repo_id"] != repo or members is not None and t["repo_id"] not in members:
            continue
        col = t["status"] if t["status"] in cols else None
        if not col:
            continue
        cols[col].append({k: t.get(k) for k in ("id", "repo_id", "title", "kind", "priority", "rank", "status", "by",
                                                "updated_at", "created_at", "where", "source", "score", "why")}
                         | {"claimed_by": t["claimed_by"] if claim_alive(t, now) else None,
                            "comments": len(t.get("comments") or []), "last_comment": (t.get("comments") or [None])[-1]})
    order = lambda c: (-(c["rank"] if c["rank"] is not None else -1e9), c["updated_at"])
    for k in cols:
        cols[k].sort(key=order) if k != "done" else cols[k].sort(key=lambda c: c["updated_at"], reverse=True)
    cols["done"] = cols["done"][:done_limit]
    return {"ok": True, "columns": cols, "agents": agents()}


@store._locked
def move(task_id: str, status: str, rank: float | None = None, by: str = "you") -> dict:
    """Move a card (the user dragging, or an agent): to a column, and optionally to a place in it."""
    t = tasks.get(task_id)
    if not t:
        raise KeyError(task_id)
    if status not in COLUMNS and status != "dropped":
        raise BoardError("status is open, working, parked, done or dropped")
    if status != t["status"]:
        t["log"].append({"at": config.now_iso(), "text": f"moved to {status} by {by}"})
        t["status"] = status
        if status in ("done", "dropped", "open", "parked"):
            t["claimed_by"] = None
    if rank is not None:
        t["rank"] = float(rank)
        t["manual_rank"] = float(rank)       # the Overseer leaves a card you placed where you put it
    t["updated_at"] = config.now_iso()
    store.save_state()
    return t


# ------------------------------------------------------------------ claims
@store._locked
def claim(task_id: str, agent: str, minutes: int = LEASE_MINUTES) -> dict:
    agent = agent_name(agent)
    t = tasks.get(task_id)
    if not t:
        raise KeyError(task_id)
    if t["status"] in ("done", "dropped"):
        raise BoardError(f"this task is {t['status']}")
    c = t.get("claimed_by") or {}
    if claim_alive(t) and c.get("agent") != agent:
        raise BoardError(f"{c['agent']} is on it (until {c['until'][11:16]} UTC): pick another task, or message them")
    minutes = max(5, min(int(minutes or LEASE_MINUTES), 8 * 60))
    t["claimed_by"] = {"agent": agent, "at": c.get("at") if c.get("agent") == agent else config.now_iso(),
                       "until": _iso(_now() + dt.timedelta(minutes=minutes))}
    if t["status"] != "working":
        t["log"].append({"at": config.now_iso(), "text": f"{agent} took it"})
        t["status"] = "working"
    t["updated_at"] = config.now_iso()
    _seen(agent, task_id)
    store.save_state()
    return t


@store._locked
def heartbeat(task_id: str, agent: str, note: str | None = None, minutes: int = LEASE_MINUTES) -> dict:
    """Still on it: extends the claim (and logs a progress note when given)."""
    agent = agent_name(agent)
    t = tasks.get(task_id)
    if not t:
        raise KeyError(task_id)
    c = t.get("claimed_by") or {}
    if c.get("agent") != agent:
        if claim_alive(t):
            raise BoardError(f"{c['agent']} has this task, not you")
        return claim(task_id, agent, minutes)     # it ran out: take it again
    c["until"] = _iso(_now() + dt.timedelta(minutes=max(5, min(int(minutes or LEASE_MINUTES), 8 * 60))))
    if note:
        t["log"].append({"at": config.now_iso(), "text": f"{agent}: {str(note)[:2000]}"})
        t["log"] = t["log"][-100:]
    t["updated_at"] = config.now_iso()
    _seen(agent, task_id)
    store.save_state()
    return t


@store._locked
def release(task_id: str, agent: str, status: str = "open", note: str | None = None) -> dict:
    """Let go of a task: done (finished and submitted), open (someone else can take it), parked (blocked)."""
    agent = agent_name(agent)
    t = tasks.get(task_id)
    if not t:
        raise KeyError(task_id)
    c = t.get("claimed_by") or {}
    if c.get("agent") not in (None, agent) and claim_alive(t):
        raise BoardError(f"{c['agent']} has this task, not you")
    if status not in ("open", "done", "parked"):
        raise BoardError("release it as open, done or parked")
    t["claimed_by"] = None
    t["log"].append({"at": config.now_iso(), "text": f"{agent} let it go: {status}" + (f" ({str(note)[:500]})" if note else "")})
    t["status"] = status
    t["updated_at"] = config.now_iso()
    _seen(agent, "")
    store.save_state()
    return t


# ------------------------------------------------------------------ comments and messages
def comment(task_id: str, by: str, text: str, to: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise BoardError("write something")
    with store.lock:
        t = tasks.get(task_id)
        if not t:
            raise KeyError(task_id)
        item = {"id": uuid.uuid4().hex[:8], "at": config.now_iso(), "by": by, "text": text[:4000], **({"to": to} if to else {})}
        t.setdefault("comments", []).append(item)
        t["comments"] = t["comments"][-200:]
        t["updated_at"] = config.now_iso()
        if by not in ("you", "overseer"):
            _seen(by)
            c = t.get("claimed_by") or {}
            if c.get("agent") == by:        # talking about it counts as being on it
                c["until"] = _iso(max(_parse(c.get("until")) or _now(), _now() + dt.timedelta(minutes=LEASE_MINUTES)))
        store.save_state()
    if to and to not in ("user", "you"):
        post(by, to, f"[on task {task_id}: {t['title'][:80]}] {text}", task_id)
    elif to in ("user", "you"):
        from . import notify
        notify.notify("message", f"{by} on “{t['title'][:60]}”", text[:500], level="info", path=f"/work?task={task_id}", repo=t["repo_id"])
    return item


def _messages() -> list[dict]:
    return store._ensure().setdefault("messages", [])


def post(sender: str, to: str, text: str, task: str | None = None) -> dict:
    text = (text or "").strip()
    if not text:
        raise BoardError("write something")
    to = (to or "").strip()
    if to not in ("*", "user", "you"):
        agent_name(to)
    msg = {"id": uuid.uuid4().hex[:10], "at": config.now_iso(), "from": sender, "to": "user" if to == "you" else to,
           "text": text[:4000], "task": task or None, "read_by": []}
    with store.lock:
        m = _messages()
        m.append(msg)
        del m[:-MESSAGES_MAX]
        if sender not in ("you", "overseer"):
            _seen(sender)
        store.save_state()
    if msg["to"] == "user":
        from . import notify
        notify.notify("message", f"Message from {sender}", text[:500], level="info", path=f"/work?task={task}" if task else "/overseer")
    return msg


def inbox(agent: str, unread_only: bool = True, mark: bool = True) -> list[dict]:
    """Messages to this agent and to everyone ("*"), oldest first."""
    me = agent if agent in ("user",) else agent_name(agent)
    with store.lock:
        out = [m for m in _messages() if m["to"] in (me, "*") and m["from"] != me and (not unread_only or me not in m["read_by"])]
        if mark:
            for m in out:
                if me not in m["read_by"]:
                    m["read_by"].append(me)
            if out:
                store.save_state()
        if me != "user":
            _seen(me)
    return [{k: v for k, v in m.items() if k != "read_by"} for m in out]


def agents() -> list[dict]:
    """Agents that used the board lately, what each is on, and how long since it was heard from."""
    now = _now()
    claimed = {}
    for t in tasks.list_tasks(status="working"):
        if claim_alive(t, now):
            claimed.setdefault(t["claimed_by"]["agent"], []).append({"id": t["id"], "title": t["title"], "repo": t["repo_id"],
                                                                     "until": t["claimed_by"]["until"]})
    out = []
    for name, a in (store._ensure().get("agents") or {}).items():
        seen = _parse(a.get("last_seen"))
        if seen and (now - seen) > dt.timedelta(days=7) and name not in claimed:
            continue
        out.append({"name": name, "last_seen": a.get("last_seen"), "on": claimed.get(name, []),
                    "minutes_since_seen": int((now - seen).total_seconds() // 60) if seen else None})
    return sorted(out, key=lambda a: a["last_seen"] or "", reverse=True)


# ------------------------------------------------------------------ agents Upstream started (herdr, via Studio)
HELD_MINUTES = 120


def hold(task_id: str, agent: str, minutes: int = HELD_MINUTES) -> None:
    """Mark a task as held by an agent Upstream started for it, and keep it held while that agent reports
    in (herdr's status, Studio's reports). Quiet: no log line each time."""
    with store.lock:
        t = tasks.get(task_id)
        if not t or t["status"] in ("done", "dropped"):
            return
        c = t.get("claimed_by") or {}
        if claim_alive(t) and c.get("agent") not in (None, agent):
            return   # an agent that claimed it on the board itself keeps it
        t["claimed_by"] = {"agent": agent, "at": c.get("at") if c.get("agent") == agent else config.now_iso(),
                           "until": _iso(_now() + dt.timedelta(minutes=minutes)), "started_by": "upstream"}
        _seen(agent, task_id)
        store.save_state()


def let_go(task_id: str, agent: str) -> None:
    with store.lock:
        t = tasks.get(task_id)
        if t and (t.get("claimed_by") or {}).get("agent") == agent:
            t["claimed_by"] = None
            _seen(agent, "")
            store.save_state()
