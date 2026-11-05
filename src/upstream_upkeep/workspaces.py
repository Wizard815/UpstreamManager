"""Merge workspaces: where a coding agent (or you) resolves merges into the focus branch.

A workspace is a git worktree next to the state file, on its own branch
``upkeep/<name>`` started from the focus branch. It merges one source (an
upstream) or a *set* of them (several feature branches, local or remote,
and upstreams) in order. Each source is pinned to the commit it had when the
workspace was opened. A clean merge is committed and the next one starts; a
conflict stops there with the conflicts left in the files. Resolve them,
then `continue` commits that merge and goes on with the rest.

The repo's own working tree is never touched. When every source is merged
and no conflict markers are left, submitting raises one approval request to
bring the branch into the focus branch (a fast-forward when the focus branch
has not moved). Nothing is pushed.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import config, gitops

_NAME = re.compile(r"merge-[A-Za-z0-9._-]{1,80}")
_MARKER = re.compile(r"^(<{7}|>{7})( |$)", re.M)
MAX_SOURCES = 12


class WorkspaceError(ValueError):
    pass


def _root(repo: dict) -> Path:
    return config.CONFIG_PATH.parent / "workspaces" / repo["id"]


def _dir(repo: dict, name: str) -> Path:
    if not _NAME.fullmatch(name or ""):
        raise WorkspaceError("bad workspace name")
    return _root(repo) / name


def path_of(repo: dict, name: str) -> Path:
    """The workspace's checkout, for the file editor."""
    ws = _dir(repo, name)
    if not ws.is_dir():
        raise WorkspaceError(f"no workspace {name}")
    return ws


def _git(cwd, *args):
    return gitops._run(["git", *args], cwd=str(cwd))


def _identity(cwd) -> list[str]:
    """Commit as the user when git knows them, else as Upstream Manager."""
    have = _git(cwd, "config", "user.email").stdout.strip()
    return [] if have else ["-c", "user.name=Upstream Manager", "-c", "user.email=upstream-manager@localhost"]


def _resolve(repo: dict, source: str) -> dict:
    """An upstream name, a local branch, or a remote branch (origin/feature) -> {label, ref, sha}."""
    focus = repo.get("focus_branch") or "main"
    up = next((u for u in repo.get("upstreams", []) if u["name"] == source), None)
    if up:
        ref = f"refs/remotes/{up['name']}/{up.get('branch') or focus}"
        if not gitops.ref_exists(repo["path"], ref):
            raise WorkspaceError(f"{source} is not fetched yet: fetch it first")
        label = f"{up['name']}/{up.get('branch') or focus}"
    elif source and not source.startswith("-") and gitops.ref_exists(repo["path"], f"refs/heads/{source}"):
        ref, label = f"refs/heads/{source}", source
    elif source and not source.startswith("-") and gitops.ref_exists(repo["path"], f"refs/remotes/{source}"):
        ref, label = f"refs/remotes/{source}", source
    else:
        raise WorkspaceError(f"{source!r} is not an upstream or a branch of this repo")
    if label == focus:
        raise WorkspaceError(f"{source} is the focus branch itself")
    return {"label": label, "ref": ref, "sha": gitops.short_sha(repo["path"], ref)}


def _checked_out_at(repo: dict, branch: str) -> str | None:
    """The worktree that has `branch` checked out, if any."""
    out = _git(repo["path"], "worktree", "list", "--porcelain").stdout
    path = None
    for line in out.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):]
        elif line == f"branch refs/heads/{branch}":
            return path
    return None


def _meta_file(ws: Path) -> Path:
    gd = _git(ws, "rev-parse", "--absolute-git-dir").stdout.strip()
    return Path(gd) / "upkeep-set.json"


def _load(ws: Path) -> dict:
    try:
        return json.loads(_meta_file(ws).read_text())
    except (OSError, ValueError):
        return {"sources": []}


def _save(ws: Path, meta: dict) -> None:
    _meta_file(ws).write_text(json.dumps(meta, indent=1))


def _unresolved(ws: Path) -> list[str]:
    out = _git(ws, "diff", "--name-only", "--diff-filter=U").stdout
    return [l for l in out.splitlines() if l.strip()]


def _markers(ws: Path, base: str, also: list[str] = ()) -> list[str]:
    """Files changed on the branch (or still unmerged) that hold conflict markers."""
    changed = set(_git(ws, "diff", "--name-only", base).stdout.split())
    changed |= set(_git(ws, "diff", "--name-only", "--cached").stdout.split())
    changed |= set(also)
    left = []
    for name in sorted(changed):
        p = ws / name
        try:
            if p.is_file() and p.stat().st_size < 5_000_000 and _MARKER.search(p.read_text(errors="ignore")):
                left.append(name)
        except OSError:
            pass
    return left


def _merging(ws: Path) -> bool:
    return _git(ws, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def _advance(ws: Path, meta: dict, focus: str) -> None:
    """Merge the next sources in order until one conflicts or all are in."""
    for src in meta["sources"]:
        if src.get("state") == "merged":
            continue
        if _merging(ws):
            return
        if _git(ws, "merge-base", "--is-ancestor", src["sha"], "HEAD").returncode == 0:
            src["state"] = "merged"  # already in (an earlier source contained it)
            continue
        msg = f"Merge {src['label']} ({src['sha'][:10]}) into {focus}"
        res = _git(ws, *_identity(ws), "merge", "--no-ff", "-m", msg, "--end-of-options", src["sha"])
        if res.returncode == 0:
            src["state"] = "merged"
            continue
        if _unresolved(ws):
            src["state"] = "conflict"
            return
        _git(ws, "merge", "--abort")
        src["state"] = "failed"
        src["error"] = (res.stderr or res.stdout).strip()[-400:]
        return


def open_workspace(repo: dict, upstream: str | None = None, branches: list[str] | None = None) -> dict:
    """Create (or return) a workspace merging `upstream`, or the set `branches`, into the focus branch."""
    focus = repo.get("focus_branch") or "main"
    if not gitops.ref_exists(repo["path"], f"refs/heads/{focus}"):
        raise WorkspaceError(f"focus branch {focus} does not exist")
    wanted = [upstream] if upstream else list(dict.fromkeys(b for b in (branches or []) if b))
    if not wanted:
        raise WorkspaceError("name an upstream or at least one branch")
    if len(wanted) > MAX_SOURCES:
        raise WorkspaceError(f"at most {MAX_SOURCES} branches in one set")
    sources = [_resolve(repo, s) for s in wanted]
    if len(sources) == 1 and upstream:
        name = f"merge-{re.sub(r'[^A-Za-z0-9._-]', '-', upstream)}-{sources[0]['sha'][:10]}"
    else:
        digest = hashlib.sha1("|".join(s["label"] + s["sha"] for s in sources).encode()).hexdigest()[:10]
        name = f"merge-set-{digest}"
    ws, branch = _dir(repo, name), f"upkeep/{name}"
    if ws.exists() and _belongs(repo, ws):
        return status(repo, name)
    if ws.exists():
        raise WorkspaceError(f"{ws} belongs to another repo with the same id; remove that folder first")
    ws.parent.mkdir(parents=True, exist_ok=True)
    _git(repo["path"], "worktree", "prune")  # forget worktrees whose folders were deleted
    elsewhere = _checked_out_at(repo, branch)
    if elsewhere:
        raise WorkspaceError(f"this merge is already open in another workspace: {elsewhere}")
    if gitops.ref_exists(repo["path"], f"refs/heads/{branch}"):
        res = _git(repo["path"], "worktree", "add", str(ws), branch)
    else:
        res = _git(repo["path"], "worktree", "add", "-b", branch, str(ws), focus)
    if res.returncode != 0:
        raise WorkspaceError((res.stderr or res.stdout).strip() or "could not create the worktree")
    meta = {"sources": [{**s, "state": "pending"} for s in sources], "focus_branch": focus,
            "base": gitops.short_sha(repo["path"], f"refs/heads/{focus}"), "created_at": config.now_iso()}
    _advance(ws, meta, focus)
    _save(ws, meta)
    return status(repo, name)


def continue_workspace(repo: dict, name: str) -> dict:
    """Finish the merge in progress (once its conflicts are resolved in the files) and merge the rest."""
    ws = path_of(repo, name)
    meta = _load(ws)
    focus = meta.get("focus_branch") or repo.get("focus_branch") or "main"
    if _merging(ws):
        unmerged = _unresolved(ws)
        left = _markers(ws, meta.get("base") or focus, unmerged)
        if left:
            raise WorkspaceError("conflict markers are still in: " + ", ".join(left[:10]))
        if unmerged:
            _git(ws, "add", "--", *unmerged)
        res = _git(ws, *_identity(ws), "commit", "--no-edit")
        if res.returncode != 0:
            raise WorkspaceError((res.stderr or res.stdout).strip()[-400:] or "could not commit the merge")
        for s in meta["sources"]:
            if s.get("state") == "conflict":
                s["state"] = "merged"
                break
    for s in meta["sources"]:
        if s.get("state") == "failed":
            s["state"] = "pending"  # try again
    _advance(ws, meta, focus)
    _save(ws, meta)
    return status(repo, name)


def status(repo: dict, name: str) -> dict:
    ws = _dir(repo, name)
    if not ws.exists():
        raise WorkspaceError(f"no workspace {name}")
    meta = _load(ws)
    focus = meta.get("focus_branch") or repo.get("focus_branch") or "main"
    branch = (_git(ws, "rev-parse", "--abbrev-ref", "HEAD").stdout or "").strip()
    merging = _merging(ws)
    # A merge committed by hand (git commit in the workspace) counts as done.
    changed = False
    for src in meta.get("sources", []):
        if src.get("state") != "merged" and not (merging and src.get("state") == "conflict") and \
                _git(ws, "merge-base", "--is-ancestor", src["sha"], "HEAD").returncode == 0:
            src["state"], changed = "merged", True
    if changed:
        _save(ws, meta)
    base = gitops.short_sha(repo["path"], f"refs/heads/{focus}") or focus
    unresolved = _unresolved(ws)
    markers = _markers(ws, base, unresolved)
    dirty = [l[3:] for l in _git(ws, "status", "--porcelain").stdout.splitlines() if l.strip()]
    behind, ahead = gitops.behind_ahead(str(ws), base, "HEAD")  # HEAD vs focus
    sources = [{k: s.get(k) for k in ("label", "sha", "state", "error")} for s in meta.get("sources", [])]
    pending = [s for s in sources if s["state"] != "merged"]
    current = next((s for s in sources if s["state"] in ("conflict", "failed")), None)
    if merging and current:
        step = (f"resolve the conflicts from {current['label']} in {len(unresolved)} file(s), "
                "then continue (or `git add` + `git commit` yourself)") if unresolved else \
               f"the merge of {current['label']} is resolved: continue to commit it and merge the rest"
    elif merging:
        step = "a merge is in progress: commit it, then continue"
    elif current and current["state"] == "failed":
        step = f"merging {current['label']} failed: {current.get('error') or 'see git'}; continue to retry"
    elif pending:
        step = f"{len(pending)} source(s) left to merge: continue"
    elif markers:
        step = "conflict markers are still in: " + ", ".join(markers[:10])
    elif dirty:
        step = "commit or discard the uncommitted changes"
    elif behind == 0:
        step = "nothing to bring in: the branch has no commits beyond the focus branch"
    else:
        step = "ready: submit it for approval"
    ready = not merging and not pending and not unresolved and not markers and not dirty and behind > 0
    return {"name": name, "path": str(ws), "branch": branch, "focus_branch": focus, "merging": merging,
            "sources": sources, "unresolved": unresolved, "conflict_markers": markers, "uncommitted": dirty[:50],
            "commits_beyond_focus": behind, "focus_commits_missing": ahead, "ready": ready, "next_step": step,
            "head": gitops.short_sha(str(ws), "HEAD")}


def _belongs(repo: dict, ws: Path) -> bool:
    """Whether a workspace folder is a worktree of this repo (not of an earlier repo with the same id)."""
    common = _git(ws, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    mine = _git(repo["path"], "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()
    return bool(common) and Path(common).resolve() == Path(mine).resolve()


def list_workspaces(repo: dict) -> list[dict]:
    root = _root(repo)
    if not root.is_dir():
        return []
    out = []
    for d in sorted(root.iterdir()):
        if _NAME.fullmatch(d.name) and _belongs(repo, d):
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
