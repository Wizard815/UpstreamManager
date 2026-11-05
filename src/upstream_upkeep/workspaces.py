"""Merge workspaces: where a coding agent (or you) resolves an upstream merge.

A workspace is a git worktree next to the state file, on its own branch
``upkeep/merge-<upstream>-<sha>`` started from the focus branch, with the
upstream merge begun and its conflicts left in the files. The repo's own
working tree is never touched. When the merge is committed and no conflict
markers are left, submitting raises an ordinary approval request to bring
that branch into the focus branch (a fast-forward when the focus branch has
not moved). Nothing is pushed.
"""
from __future__ import annotations

import re
from pathlib import Path

from . import config, gitops

_NAME = re.compile(r"merge-[A-Za-z0-9._-]{1,80}")
_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.M)


class WorkspaceError(ValueError):
    pass


def _root(repo: dict) -> Path:
    return config.CONFIG_PATH.parent / "workspaces" / repo["id"]


def _dir(repo: dict, name: str) -> Path:
    if not _NAME.fullmatch(name or ""):
        raise WorkspaceError("bad workspace name")
    return _root(repo) / name


def _git(cwd, *args):
    return gitops._run(["git", *args], cwd=str(cwd))


def _upstream_ref(repo: dict, upstream: str) -> tuple[dict, str]:
    up = next((u for u in repo.get("upstreams", []) if u["name"] == upstream), None)
    if not up:
        raise WorkspaceError(f"no upstream named {upstream}")
    ref = f"refs/remotes/{up['name']}/{up.get('branch') or repo.get('focus_branch')}"
    if not gitops.ref_exists(repo["path"], ref):
        raise WorkspaceError(f"{upstream} is not fetched yet: fetch it first")
    return up, ref


def _unresolved(ws: Path) -> list[str]:
    out = _git(ws, "diff", "--name-only", "--diff-filter=U").stdout
    return [l for l in out.splitlines() if l.strip()]


def _markers(ws: Path, base: str) -> list[str]:
    """Files changed on the branch that still hold conflict markers."""
    changed = set(_git(ws, "diff", "--name-only", base).stdout.split())
    changed |= set(_git(ws, "diff", "--name-only", "--cached").stdout.split())
    left = []
    for name in sorted(changed):
        p = ws / name
        try:
            if p.is_file() and p.stat().st_size < 5_000_000 and _MARKER.search(p.read_text(errors="ignore")):
                left.append(name)
        except OSError:
            pass
    return left


def open_workspace(repo: dict, upstream: str) -> dict:
    """Create (or return) the workspace for merging `upstream` into the focus branch."""
    focus = repo.get("focus_branch") or "main"
    if not gitops.ref_exists(repo["path"], f"refs/heads/{focus}"):
        raise WorkspaceError(f"focus branch {focus} does not exist")
    up, ref = _upstream_ref(repo, upstream)
    target = gitops.short_sha(repo["path"], ref)
    name = f"merge-{up['name']}-{target[:10]}"
    ws, branch = _dir(repo, name), f"upkeep/{name}"
    if ws.exists():
        return status(repo, name)
    ws.parent.mkdir(parents=True, exist_ok=True)
    if gitops.ref_exists(repo["path"], f"refs/heads/{branch}"):
        res = _git(repo["path"], "worktree", "add", str(ws), branch)
    else:
        res = _git(repo["path"], "worktree", "add", "-b", branch, str(ws), focus)
    if res.returncode != 0:
        raise WorkspaceError((res.stderr or res.stdout).strip() or "could not create the worktree")
    msg = f"Merge {up['name']}/{up.get('branch') or focus} ({target[:10]}) into {focus}"
    merge = _git(ws, "merge", "--no-ff", "--no-commit", "-m", msg, target)
    if merge.returncode != 0 and not _unresolved(ws) and "Already up to date" not in merge.stdout:
        remove(repo, name)
        raise WorkspaceError((merge.stderr or merge.stdout).strip()[-500:] or "merge could not start")
    return status(repo, name)


def status(repo: dict, name: str) -> dict:
    ws = _dir(repo, name)
    if not ws.exists():
        raise WorkspaceError(f"no workspace {name}")
    focus = repo.get("focus_branch") or "main"
    branch = (_git(ws, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
    merging = _git(ws, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    base = gitops.short_sha(repo["path"], f"refs/heads/{focus}") or focus
    unresolved = _unresolved(ws)
    markers = _markers(ws, base)
    dirty = [l[3:] for l in _git(ws, "status", "--porcelain").stdout.splitlines() if l.strip()]
    behind, ahead = gitops.behind_ahead(str(ws), base, "HEAD")  # HEAD vs focus
    ready = not merging and not unresolved and not markers and not dirty and behind > 0
    if merging:
        step = (f"resolve {len(unresolved)} conflicted file(s), then `git add` them and `git commit`"
                if unresolved else "review the merged files, run the tests, then `git commit` to finish the merge")
    elif markers:
        step = "conflict markers are still in: " + ", ".join(markers[:10])
    elif dirty:
        step = "commit or discard the uncommitted changes"
    elif behind == 0:
        step = "nothing to bring in: the branch has no commits beyond the focus branch"
    else:
        step = "ready: submit it for approval"
    return {"name": name, "path": str(ws), "branch": branch, "focus_branch": focus, "merging": merging,
            "unresolved": unresolved, "conflict_markers": markers, "uncommitted": dirty[:50],
            "commits_beyond_focus": behind, "focus_commits_missing": ahead, "ready": ready, "next_step": step,
            "head": gitops.short_sha(str(ws), "HEAD")}


def list_workspaces(repo: dict) -> list[dict]:
    root = _root(repo)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if _NAME.fullmatch(d.name):
            try:
                out.append(status(repo, d.name))
            except Exception as e:  # a worktree deleted by hand
                out.append({"name": d.name, "path": str(d), "error": str(e)})
    return out


def remove(repo: dict, name: str) -> None:
    ws = _dir(repo, name)
    branch = f"upkeep/{name}"
    _git(repo["path"], "worktree", "remove", "--force", str(ws))
    _git(repo["path"], "worktree", "prune")
    if gitops.ref_exists(repo["path"], f"refs/heads/{branch}"):
        _git(repo["path"], "branch", "-D", branch)
