"""Disk use and cleanup of Upstream Manager's own data.

What it measures: its data folder (state, memory, workspaces), each repo's git folder and the
history-only copies of components it keeps inside it. What it cleans is only its own leftovers:
old decided requests, component copies nothing uses, workspace folders of removed repos.
It never touches your files, branches or commits."""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import components, config, gitops, store

KEEP_DECIDED_DAYS = 30


def _size(p: Path) -> int:
    """Bytes under a path, without following links; unreadable parts count as 0."""
    total, stack = 0, [p]
    while stack:
        cur = stack.pop()
        try:
            if cur.is_symlink() or cur.is_file():
                total += cur.lstat().st_size
                continue
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        else:
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        pass
        except OSError:
            pass
    return total


def _data_dir() -> Path:
    return config.CONFIG_PATH.parent


def _mirrors_dir(repo_path: str) -> Path | None:
    git = Path(repo_path) / ".git"
    return git / "upkeep-components" if git.is_dir() else None


def _orphan_mirrors(repo: dict) -> list[Path]:
    """Component copies for components the repo no longer lists, or that are checked out now."""
    d = _mirrors_dir(repo["path"])
    if not d or not d.is_dir():
        return []
    used = {components._mirror(repo["path"], c).name for c in repo.get("components") or []
            if not components.initialized(repo["path"], c)}
    return [m for m in d.iterdir() if m.name not in used]


def _old_decided(now: datetime | None = None) -> list[dict]:
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=KEEP_DECIDED_DAYS)
    out = []
    for r in store.get_requests():
        if r.get("status") == "pending" or not r.get("decided_at"):
            continue
        try:
            when = datetime.fromisoformat(r["decided_at"].replace("Z", "+00:00"))
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        if when < cutoff:
            out.append(r)
    return out


def _orphan_workspaces() -> list[Path]:
    root = _data_dir() / "workspaces"
    ids = {r["id"] for r in store.all_repos()}
    return [d for d in root.iterdir() if d.is_dir() and d.name not in ids] if root.is_dir() else []


def usage() -> dict:
    data = _data_dir()
    app = {name: _size(data / name) for name in ("state.json", "workspaces")}
    app["memory"] = sum(_size(p) for p in data.glob("memory.db*"))
    repos = []
    for r in store.all_repos():
        path = Path(r["path"])
        if not path.is_dir():
            repos.append({"id": r["id"], "name": r.get("name"), "path": str(path), "missing": True})
            continue
        mirrors = _mirrors_dir(r["path"])
        m = _size(mirrors) if mirrors and mirrors.is_dir() else 0
        git = path / ".git"
        ws = data / "workspaces" / r["id"]
        repos.append({"id": r["id"], "name": r.get("name"), "path": str(path),
                      "git": max(0, _size(git) - m) if git.is_dir() else 0,
                      "component_copies": m,
                      "workspaces": _size(ws) if ws.is_dir() else 0,
                      "partial": gitops.is_partial(str(path))})
    repos.sort(key=lambda x: -(x.get("git", 0) + x.get("component_copies", 0)))
    clean = cleanup(dry_run=True)
    return {"data_dir": str(data), "app": app, "app_total": sum(app.values()), "repos": repos, "cleanup": clean}


def cleanup(dry_run: bool = False, what: list[str] | None = None) -> dict:
    """Remove Upstream Manager's own leftovers. what: any of requests, component_copies, workspaces."""
    what = set(what or ("requests", "component_copies", "workspaces"))
    out = {"dry_run": dry_run, "requests": [], "component_copies": [], "workspaces": [], "freed": 0}
    if "requests" in what:
        old = _old_decided()
        out["requests"] = [r["id"] for r in old]
        if old and not dry_run:
            store.forget_requests({r["id"] for r in old})
    if "component_copies" in what:
        for r in store.all_repos():
            if not Path(r["path"]).is_dir():
                continue
            for m in _orphan_mirrors(r):
                size = _size(m)
                out["component_copies"].append({"repo": r["id"], "name": re.sub(r"\.git$", "", m.name), "bytes": size})
                out["freed"] += size
                if not dry_run:
                    shutil.rmtree(m, ignore_errors=True)
    if "workspaces" in what:
        for d in _orphan_workspaces():
            size = _size(d)
            out["workspaces"].append({"folder": d.name, "bytes": size})
            out["freed"] += size
            if not dry_run:
                shutil.rmtree(d, ignore_errors=True)
    return out
