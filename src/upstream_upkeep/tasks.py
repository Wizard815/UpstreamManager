"""Tasks: a piece of work about a repo (fix a commit, a bug, a new change, a merge) and where it lives.

A task is created from anywhere in the UI ("Work on this") or by an agent, and is linked to what it
is about: commits, requests, files, components. It then goes one of these ways:

- here:   worked on in Upstream Manager: a chat about it and, for code, its own change workspace
          (a branch), submitted as an approval request like any other change;
- hermes: handed to a Hermes agent session for longer, bigger problems (see hermes.py);
- wiki:   parked in the LLM wiki's short-term tier, for later (see wiki.park);
- orb:    parked as a task in Pondering Orb (see orb.py).

Tasks are kept in state.json next to requests. Nothing here changes the repo by itself."""
from __future__ import annotations

import uuid

from . import config, store, workspaces

KINDS = ("fix", "bug", "feature", "merge", "chore", "idea")
STATUSES = ("open", "working", "parked", "done", "dropped")
WHERE = ("here", "hermes", "wiki", "orb")
ABOUT_TYPES = ("commit", "request", "file", "component", "pr", "push", "conflict")
MAX_ABOUT = 12


class TaskError(ValueError):
    pass


def _all() -> list[dict]:
    return store._ensure().setdefault("tasks", [])


def _clean_about(about) -> list[dict]:
    if about is None:
        return []
    if not isinstance(about, list):
        raise TaskError("about must be a list")
    out = []
    for a in about[:MAX_ABOUT]:
        if not isinstance(a, dict) or a.get("type") not in ABOUT_TYPES or not str(a.get("id") or "").strip():
            raise TaskError("each about item needs a type (" + ", ".join(ABOUT_TYPES) + ") and an id")
        item = {"type": a["type"], "id": str(a["id"])[:300], "label": str(a.get("label") or a["id"])[:200]}
        for k in ("repo", "url"):
            if isinstance(a.get(k), str) and a[k]:
                item[k] = a[k][:500]
        out.append(item)
    return out


def _text(v, field: str, limit: int, required: bool = False) -> str:
    if v is None:
        v = ""
    if not isinstance(v, str):
        raise TaskError(f"{field} must be text")
    v = v.strip()
    if required and not v:
        raise TaskError(f"{field} is required")
    return v[:limit]


def list_tasks(repo_id: str | None = None, status: str | None = None) -> list[dict]:
    out = [t for t in _all() if (not repo_id or t["repo_id"] == repo_id) and (not status or t["status"] == status)]
    return sorted(out, key=lambda t: t["updated_at"], reverse=True)


def get(task_id: str) -> dict | None:
    return next((t for t in _all() if t["id"] == task_id), None)


@store._locked
def create(repo_id: str, title, body=None, kind: str = "fix", about=None, by: str = "user", upstream: str | None = None) -> dict:
    if repo_id != store.GENERAL and not store.get_repo(repo_id):
        raise TaskError(f"no repo {repo_id}")
    if kind not in KINDS:
        raise TaskError("kind must be one of " + ", ".join(KINDS))
    now = config.now_iso()
    t = {"id": uuid.uuid4().hex[:12], "repo_id": repo_id, "title": _text(title, "title", 200, True),
         "body": _text(body, "body", 20000), "kind": kind, "about": _clean_about(about),
         "status": "open", "where": None, "created_at": now, "updated_at": now, "by": by,
         "log": [{"at": now, "text": f"created by {'an agent' if by == 'agent' else 'you'}"}]}
    if kind == "merge" and isinstance(upstream, str) and upstream.strip():
        t["upstream"] = upstream.strip()[:100]  # a merge task works in a merge workspace for this upstream
    _all().append(t)
    store.save_state()
    return t


@store._locked
def update(task_id: str, **fields) -> dict:
    t = get(task_id)
    if not t:
        raise KeyError(task_id)
    if "title" in fields and fields["title"] is not None:
        t["title"] = _text(fields["title"], "title", 200, True)
    if "body" in fields and fields["body"] is not None:
        t["body"] = _text(fields["body"], "body", 20000)
    if fields.get("kind") is not None:
        if fields["kind"] not in KINDS:
            raise TaskError("kind must be one of " + ", ".join(KINDS))
        t["kind"] = fields["kind"]
    if fields.get("about") is not None:
        t["about"] = _clean_about(fields["about"])
    if fields.get("status") is not None:
        if fields["status"] not in STATUSES:
            raise TaskError("status must be one of " + ", ".join(STATUSES))
        if fields["status"] != t["status"]:
            t["log"].append({"at": config.now_iso(), "text": f"status: {t['status']} → {fields['status']}"})
        t["status"] = fields["status"]
    if fields.get("where") is not None:
        t["where"] = fields["where"]
    if fields.get("note"):
        t["log"].append({"at": config.now_iso(), "text": str(fields["note"])[:2000]})
        t["log"] = t["log"][-100:]
    t["updated_at"] = config.now_iso()
    store.save_state()
    return t


@store._locked
def delete(task_id: str) -> bool:
    tasks = _all()
    n = len(tasks)
    tasks[:] = [t for t in tasks if t["id"] != task_id]
    store.save_state()
    return len(tasks) != n


def brief(task: dict, repo: dict | None) -> str:
    """The task as Markdown, for a chat's first message, a Hermes session or a parked note."""
    lines = [f"# {task['title']}", "", f"Kind: {task['kind']}"]
    if repo:
        lines.append(f"Repo: {repo.get('name') or repo['id']} at {repo['path']} (focus branch {repo.get('focus_branch') or 'main'})")
    if task["about"]:
        lines.append("About:")
        lines += [f"- {a['type']} {a['label']}" + (f" ({a['id']})" if a["id"] != a["label"] else "") + (f" {a['url']}" if a.get("url") else "")
                  for a in task["about"]]
    if task["body"]:
        lines += ["", task["body"]]
    return "\n".join(lines)


CODE_KINDS = ("fix", "bug", "feature", "chore", "merge")
SESSION_TAG_TYPES = ("commit", "pr", "request", "push")


def _workspace(task: dict, repo: dict) -> dict | None:
    """The task's own change workspace (a branch off the focus branch), made once and reused."""
    if task["kind"] not in CODE_KINDS or repo is None:
        return None
    w = task.get("workspace")
    if w:
        try:
            return workspaces.status(repo, w)
        except Exception:
            pass
    if task.get("upstream"):
        return workspaces.open_workspace(repo, upstream=task["upstream"])
    return workspaces.open_workspace(repo, change=f"{task['title']} {task['id'][:4]}")


def send_to(task_id: str, to: str, message: str | None = None) -> dict:
    """Send a task where it will be worked on or kept. Returns what the UI needs to follow it."""
    from . import hermes, orb, wiki  # integrations, loaded when used
    task = get(task_id)
    if not task:
        raise KeyError(task_id)
    if to not in WHERE:
        raise TaskError("to must be one of " + ", ".join(WHERE))
    repo = store.get_repo(task["repo_id"])
    if to == "here":
        ws = _workspace(task, repo)
        session = store.new_session(task["repo_id"])
        tags = [{k: a[k] for k in ("type", "id", "label", "repo", "url") if k in a}
                for a in task["about"] if a["type"] in SESSION_TAG_TYPES]
        if tags:
            store.set_session_tags(task["repo_id"], session["id"], tags)
        store.mark_task_session(task["repo_id"], session["id"], task["title"], task_id)
        where = {"type": "here", "session": session["id"], "workspace": ws and ws["name"]}
        task["workspace"] = ws and ws["name"]
        update(task_id, status="working", where=where, note="working on it here" + (f" in workspace {ws['name']}" if ws else ""))
        draft = brief(task, repo) + ("\n\nWork in the workspace " + ws["path"] + " (its own branch)." if ws else "")
        return {"where": where, "draft": draft}
    if to == "hermes":
        if repo is None:
            raise TaskError("Hermes works on a repo: this task is not about one")
        ws = _workspace(task, repo)
        workdir = ws["path"] if ws else repo["path"]
        sid = (task.get("where") or {}).get("hermes")
        if sid and hermes.run_state(sid).get("running"):
            raise TaskError("Hermes is still working on this task")
        if not sid:
            sid = hermes.start(task["title"], hermes.instructions(repo, workdir))
        def done(err, tid=task_id):
            try:
                update(tid, note=f"Hermes failed: {err}" if err else "Hermes replied")
            except KeyError:
                pass
        hermes.send(sid, message or brief(task, repo), on_done=done)
        where = {"type": "hermes", "hermes": sid, "workspace": ws and ws["name"], "workdir": workdir}
        task["workspace"] = ws and ws["name"]
        update(task_id, status="working", where=where, note="handed to Hermes")
        return {"where": where}
    if to == "wiki":
        res = wiki.park(task["title"], brief(task, repo) + f"\n\n(Parked from Upstream Manager, task {task['id']}.)")
        where = {"type": "wiki", **res}
        update(task_id, status="parked", where=where, note=f"parked in the LLM wiki: {res['note']}")
        return {"where": where}
    res = orb.add_task(task["title"], brief(task, repo) + f"\n\n(From Upstream Manager, task {task['id']}.)",
                       priority={"bug": 6, "fix": 5}.get(task["kind"], 3))
    where = {"type": "orb", **res}
    update(task_id, status="parked", where=where, note=f"parked in Pondering Orb ({res['list']})")
    return {"where": where}
