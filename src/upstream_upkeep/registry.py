"""Repo registry: watch folders, git-URL repos, per-repo branch focus + multiple upstreams.

A Repo entry describes what Upstream Management tracks:

  id          stable slug (used as the HTML id / API key)
  path        local working-tree root (for a URL repo we clone into the watch folder)
  name        repo display name
  kind        'folder' | 'url' | 'url-cloned'
  upstream_url optional, when the repo was added by URL
  focus_branch the branch the chat UI is "looking at" right now
  watch       whether to include this repo in the daily cron sweep
  upstreams   list of upstreams to compare the focus branch against
      - name          the git remote (e.g. 'origin')
      - url            its URL
      - branch         the remote-tracking branch to compare to (e.g. 'main')
      - enabled        include this upstream in checks

Upstreams can be added/removed/rebased. 'Rebase' = refresh the remote-tracking ref
(fetch) and recompute ahead/behind.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from urllib.parse import urlparse

from . import components, gitops
from .config import PROJECT_ROOT
from . import store
from . import upstream_check

WATCH_FOLDERS = [
    Path("/home/codebox/Repos"),
    PROJECT_ROOT / "watch",
    Path("/home/codebox/OnePiece"),
]


def slugify(text: str) -> str:
    text = (text or "").strip().lower()
    text = re.sub(r"[^a-z0-9._-]+", "-", text)
    text = re.sub(r"-+", "-", text).strip("-")
    return text or "repo"


def parse_git_url(url: str) -> str:
    """Validate / normalise a git URL. Returns the URL or raises ValueError."""
    url = (url or "").strip()
    if not url:
        raise ValueError("git URL is required")
    if url.endswith(".git"):
        url = url[:-4]
    # allow https://host/org/repo, git@host:org/repo, ssh://...
    patterns = [
        r"^git@[^\s:/][^\s]*:[^\s]+\.git?$",  # scp-like: git@github.com:org/repo
        r"^https?://[^\s]+$",                 # http(s)://...
        r"^ssh://[^\s]+",                      # ssh://...
    ]
    if not any(re.match(p, url) for p in patterns):
        raise ValueError(f"looks like not a git URL: {url!r}")
    if "://" not in url and not url.startswith("git@"):
        url = url.lstrip("/\\")
        if "/" in url and not url.startswith("git@"):
            url = "https://" + url
    if not url.endswith(".git"):
        url = url.rstrip("/") + ".git"
    return url


def repo_dir_for_url(watch: Path, url: str) -> str:
    parsed = urlparse(url)
    name = parsed.path.rstrip("/").split("/")[-1] or "clone"
    return os.path.join(str(watch), name)


def discover_from_watch_folders() -> list[dict]:
    """Scan configured watch folders for existing git repos and return repo dicts.

    Used on startup / when the user says 'move a repo into the watch folder'.
    """
    found: list[dict] = []
    for wf in WATCH_FOLDERS:
        if not wf.exists():
            continue
        for entry in sorted(Path(wf).iterdir()):
            if entry.is_dir() and gitops.is_git_repo(str(entry)):
                remotes = gitops.remotes(str(entry))
                focus = gitops.current_branch(str(entry))
                found.append({
                    "name": entry.name,
                    "path": str(entry),
                    "kind": "folder",
                    "focus_branch": focus,
                    "remote_url": remotes.get("origin"),
                    "upstreams": [
                        {
                            "name": name,
                            "url": url,
                            "branch": focus,
                            "enabled": True,
                        }
                        for name, url in remotes.items()
                    ],
                })
    return found


def add_repo(path: str | None, url: str | None, name: str | None,
             focus_branch: str | None = None, watch_dir: str | None = None) -> dict:
    """Add a repo by local path OR by git URL. Clones URL repos into a watch folder."""
    if path:
        if not gitops.is_git_repo(path):
            raise ValueError(f"not a git repo: {path}")
        p = Path(os.path.abspath(path)).resolve()
        remotes = gitops.remotes(str(p))
        fb = focus_branch or gitops.current_branch(str(p))
        rid = slugify(name or p.name)
        repo = {
            "id": rid,
            "name": name or p.name,
            "path": str(p),
            "kind": "folder",
            "focus_branch": fb,
            "remote_url": remotes.get("origin"),
            "watch": True,
            "upstreams": [
                {"name": n, "url": u, "branch": fb, "enabled": True}
                for n, u in remotes.items()
            ],
        }
        repo["components"] = components.sync({**(store.get_repo(repo["id"]) or {}), **repo})
        return store.upsert_repo(repo)

    if url:
        norm = parse_git_url(url)
        wd = Path(watch_dir) if watch_dir else WATCH_FOLDERS[0]
        wd.mkdir(parents=True, exist_ok=True)
        dest = repo_dir_for_url(wd, norm)
        if not (os.path.exists(dest) and gitops.is_git_repo(dest)):
            gitops.clone_into(dest, norm)
        remotes = gitops.remotes(dest)
        fb = focus_branch or gitops.current_branch(dest)
        rid = slugify(name or Path(dest).name)
        repo = {
            "id": rid,
            "name": name or Path(dest).name,
            "path": dest,
            "kind": "url-cloned",
            "focus_branch": fb,
            "remote_url": norm,
            "watch": True,
            "upstreams": [
                {"name": n, "url": u, "branch": fb, "enabled": True}
                for n, u in remotes.items()
            ],
        }
        repo["components"] = components.sync({**(store.get_repo(repo["id"]) or {}), **repo})
        return store.upsert_repo(repo)

    raise ValueError("provide path= or url=")


def add_url(repo_id: str, remote_name: str, remote_url: str,
            focus_branch: str | None = None) -> dict | None:
    """Add a single upstream to an existing tracked repo (multi-upstream watching)."""
    repo = _get_repo(repo_id)
    if not repo:
        return None
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", remote_name or ""):
        raise ValueError("remote name may only use letters, digits, '.', '_' and '-'")
    remotes = gitops.remotes(repo["path"])
    remote_url = remote_url.replace(" (fetch)", "").replace(" (push)", "")
    focus = focus_branch or repo.get("focus_branch") or gitops.current_branch(repo["path"])
    existing = remotes.get(remote_name)
    if not existing:
        gitops._run(["git", "remote", "add", remote_name, remote_url], cwd=repo["path"])
        remotes[remote_name] = remote_url
    up = {"name": remote_name, "url": remote_url, "branch": focus, "enabled": True}
    with store.lock:
        repo["upstreams"] = [u for u in repo.get("upstreams", []) if u.get("name") != remote_name] + [up]
        _save_repo(repo)
    return repo


def remove_upstream(repo_id: str, remote_name: str) -> dict | None:
    """Remove an upstream (and drop its git remote) from a tracked repo."""
    repo = _get_repo(repo_id)
    if not repo:
        return None
    if remote_name not in {u.get("name") for u in repo.get("upstreams", [])}:
        return None
    gitops._run(["git", "remote", "remove", remote_name], cwd=repo["path"])
    with store.lock:
        repo["upstreams"] = [u for u in repo.get("upstreams", []) if u.get("name") != remote_name]
        _save_repo(repo)
    return repo


def set_focus_branch(repo_id: str, branch: str) -> dict | None:
    """Set the branch you work on. Each upstream keeps its own branch to compare against."""
    with store.lock:
        repo = _get_repo(repo_id)
        if not repo:
            return None
        repo["focus_branch"] = branch
        _save_repo(repo)
    return repo


def remove_repo(repo_id: str) -> bool:
    repo = _get_repo(repo_id)
    if not repo:
        return False
    _save_repo({"id": repo_id, "deleted": True})
    return True


def set_watch(repo_id: str, enabled: bool) -> dict | None:
    repo = _get_repo(repo_id)
    if not repo:
        return None
    with store.lock:
        repo["watch"] = enabled
        _save_repo(repo)
    return repo


def get_repo(repo_id: str) -> dict | None:
    return store.get_repo(repo_id)


def _get_repo(repo_id: str) -> dict | None:
    return store.get_repo(repo_id)


def _save_repo(repo: dict) -> dict:
    from .store import save_repo
    return save_repo(repo)


def upstream_focus_repo(repo: dict, remote_name: str) -> dict | None:
    for up in repo.get("upstreams", []):
        if up.get("name") == remote_name:
            return up
    return None


def upstream_for_branch(repo: dict, upstream_url: str) -> dict | None:
    for up in repo.get("upstreams", []):
        if up.get("url") == upstream_url:
            return up
    return None


def repo_summary() -> list[dict]:
    """Per-repo summary for the list view, from refs already in each repo (no network)."""
    out = []
    for repo in store.all_repos():
        ups, conflicts, error = [], [], None
        try:
            plan = upstream_check.build_merge_plan(
                repo["path"], repo["id"], repo["name"], repo.get("upstreams", []),
                focus_branch=repo.get("focus_branch"),
            )
            ups = [u.to_dict() for u in plan.upstreams]
            conflicts = plan.conflicts
        except Exception as e:
            error = str(e)
        commits = gitops.recent_commits(repo["path"], 1)
        out.append({
            "id": repo["id"],
            "name": repo.get("name"),
            "path": repo.get("path"),
            "kind": repo.get("kind"),
            "focus_branch": repo.get("focus_branch"),
            "watch": repo.get("watch", True),
            "behind": sum(u["behind"] for u in ups),
            "ahead": sum(u["ahead"] for u in ups),
            "conflicts": conflicts,
            "upstream_details": ups,
            "components": _component_summary(repo),
            "upstreams": [u["name"] for u in repo.get("upstreams", [])],
            "last_commit": commits[0] if commits else None,
            "dirty": gitops.status_summary(repo["path"])["dirty"] if not error else 0,
            "error": error,
        })
    return out


def repo_by_path(path: str) -> dict | None:
    """The tracked repo at `path`, if any (so path-taking endpoints stay inside tracked repos)."""
    try:
        want = Path(path).resolve()
    except (OSError, ValueError):
        return None
    for r in store.all_repos():
        if Path(r["path"]).resolve() == want:
            return r
    return None


def _component_summary(repo: dict) -> list[dict]:
    """Per-component drift for the repo list (no network)."""
    try:
        return [{k: c.get(k) for k in ("name", "path", "status", "behind", "local", "enabled", "watched_changes")}
                for c in components.compare_all(repo)]
    except Exception as e:  # never break the list over one hub
        return [{"name": "components", "status": "error", "detail": str(e)}]


def get_watching_repos() -> list[dict]:
    return [r for r in store.all_repos() if r.get("watch", True)]
