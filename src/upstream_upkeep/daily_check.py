"""Daily check engine: detect -> optionally commit -> record -> optionally push.

The approval gate is the safety core of this app. The daily run is fully
automated for *detecting* and *committing locally*, but pushing is gated: it
only happens when the user has approved that specific commit in the UI.

Flow:
  1. snapshot the repo
  2. if there are changes and we haven't already committed today, commit locally
     (staging + commit). We NEVER merge upstream into the branch automatically.
  3. store the proposed commit (sha + message + what changed) in repo.pending_pushes
  4. the UI presents it; on approval we run the merge + push and clear the pending entry.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from . import gitops
from .change_engine import build_commit_message
from .config import now_iso
from .types import DailyRunResult


def _prepare_branch(path: str, remote: str | None) -> bool:
    """Bring local branch up to date with upstream (no fast-forwarding issues),
    then ensure it's ahead of upstream by rebasing. Returns True on success."""
    if remote is None:
        return True
    base = f"refs/remotes/{remote}/HEAD"
    try:
        out = gitops._git(["rev-list", "--count", f"HEAD..{base}"], path)
    except Exception:
        out = "0"
    if not out or int(out) == 0:
        return True
    # fetch latest upstream
    gitops._git(["fetch", remote], path)
    # merge upstream into local branch (user can amend/cancel)
    res = gitops._run(["git", "merge", "--ff-only", base], cwd=path, check=False)
    if res.returncode != 0:
        # fall back to a merge commit
        gitops._run(["git", "merge", base], cwd=path, check=False)
    return True


def run_daily_check(
    path: str,
    remote: str | None,
    window_days: int = 2,
    commit: bool = True,
    commit_message_override: str | None = None,
) -> DailyRunResult:
    result: DailyRunResult = {
        "path": path,
        "timestamp": now_iso(),
        "status": "ok",
        "error": None,
        "upstream_count": 0,
        "recent_count": 0,
        "dirty": False,
        "has_local_changes": False,
        "committed_sha": None,
        "commit_message": None,
        "committed": False,
        "ready_to_push": False,
        "pushed": False,
        "merge_conflict": False,
        "push_output": None,
    }

    if not gitops.is_git_repo(path):
        result["status"] = "error"
        result["error"] = "not a git repo"
        return result

    snapshot = None
    try:
        snapshot = snapshot_repo(path, remote, window_days)
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result
    result["upstream_count"] = snapshot.upstream_count
    result["recent_count"] = snapshot.recent_count
    result["dirty"] = snapshot.dirty

    if snapshot.dirty:
        result["has_local_changes"] = True

    # 1. prepare the branch to be pushable (merge upstream first)
    if not _prepare_branch(path, remote):
        result["status"] = "error"
        result["error"] = "failed to prepare branch"
        return result

    # 2. stage + commit local changes
    if commit and snapshot.dirty:
        gitops.stage_all(path)
        try:
            msg = commit_message_override or build_commit_message(snapshot, snapshot.repo_name or "repo")
            sha = gitops.commit(path, msg)
            if sha:
                result["committed_sha"] = sha
                result["commit_message"] = msg
                result["committed"] = True
                result["ready_to_push"] = True
                result["merge_conflict"] = False
            else:
                result["status"] = "error"
                result["error"] = "commit failed (nothing staged or author identity missing)"
        except Exception as e:
            result["status"] = "error"
            result["error"] = str(e)

    return result


def run_push(path: str, remote: str | None, branch: str | None, message: str) -> DailyRunResult:
    """Merge upstream into the branch then push. Returns the push result."""
    result: DailyRunResult = {
        "path": path,
        "timestamp": now_iso(),
        "status": "ok",
        "error": None,
        "pushed": False,
        "push_output": None,
        "message": message,
    }
    if remote is None:
        result["error"] = "no upstream remote configured"
        result["status"] = "error"
        return result
    try:
        base = f"refs/remotes/{remote}/HEAD"
        gitops._git(["fetch", remote], path)
        res = gitops._run(["git", "merge", "--ff-only", base], cwd=path, check=False)
        if res.returncode != 0:
            res = gitops._run(["git", "merge", base], cwd=path, check=False)
        if res.returncode != 0:
            result["status"] = "error"
            result["error"] = f"merge failed: {res.stderr.strip()}"
            return result
        push = gitops.push(path, remote, branch, message)
        result["push_output"] = push["output"]
        result["pushed"] = push["ok"]
        result["status"] = "ok" if push["ok"] else "error"
        if not push["ok"]:
            result["error"] = push["output"] or "push failed"
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
    return result


def merge_upstream(path: str, upstream: dict) -> dict:
    """Merge a single upstream into the focus branch. Returns {ok, detail}.
    This is the auto-merge step (safe plans only) after approval."""
    remote = upstream["name"]
    branch = upstream.get("branch") or "main"
    base = f"refs/remotes/{remote}/{branch}"
    try:
        res = gitops._git(["rev-list", "--count", f"{base}..HEAD"], path)
        ahead = int(res or 0)
    except Exception:
        ahead = 0
    if ahead == 0:
        return {"ok": True, "detail": "nothing to merge"}
    gitops._git(["fetch", "-f", remote, f"+refs/heads/*:refs/remotes/{remote}/*"], path)
    res = gitops._run(["git", "merge", "--no-ff", base], cwd=path, check=False)
    if res.returncode != 0:
        gitops._run(["git", "merge", "--abort"], cwd=path)
        return {"ok": False, "detail": res.stderr.strip() or res.stdout.strip()}
    return {"ok": True, "detail": f"merged {ahead} commit(s) from {remote}/{branch}"}
