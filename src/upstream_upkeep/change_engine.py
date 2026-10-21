"""Change detection and the daily check engine.

Given a repo, produces three independent signals:

  * upstream changes   - commits the remote has that we don't (the "upstream" signal)
  * recent changes     - local commits / untracked files from the last window_days
  * dirty state        - whether the working tree is clean

These feed the UI (repo list shows counts) and the daily check (which builds a
commit message from what changed).
"""
from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime, timezone, timedelta

from . import gitops
from .gitops import remote_ahead_count

RECENT_WINDOW_DAYS = 2
DEFAULT_REMOTE = "upstream"


@dataclass
class RepoSnapshot:
    path: str
    repo_name: str | None
    upstream_url: str | None
    base_ref: str | None
    local_sha: str | None
    upstream_sha: str | None
    recent_sha: str | None
    upstream_count: int
    recent_count: int
    dirty: bool
    dirty_with_upstream_pending: bool
    working_tree: str
    ahead_by: int
    behind_by: int
    upstream_branch: str | None
    last_commit: str | None
    error: str | None = None

    def summary(self) -> str:
        parts = []
        if self.upstream_count:
            parts.append(f"upstream {self.upstream_count}")
        if self.recent_count:
            parts.append(f"recent {self.recent_count}")
        if self.dirty:
            parts.append("dirty")
        if not parts:
            return "up to date"
        return " · ".join(parts)


def _remote_for(path: str, remote_pref: str | None) -> str | None:
    return gitops.resolve_remote_name(path, remote_pref)


def snapshot_repo(path: str, remote_pref: str | None, window_days: int = RECENT_WINDOW_DAYS) -> RepoSnapshot:
    if not gitops.is_git_repo(path):
        return RepoSnapshot(path=path, repo_name="not-a-repo", upstream_url=None, base_ref=None,
                            local_sha=None, upstream_sha=None, recent_sha=None, upstream_count=0,
                            recent_count=0, dirty=False, dirty_with_upstream_pending=False,
                            working_tree="not-a-repo", ahead_by=0, behind_by=0, upstream_branch=None,
                            last_commit=None, error="not a git repo")

    name = gitops.repo_name(path)
    remote = _remote_for(path, remote_pref)
    base_ref = gitops.default_ref(path) or gitops.current_branch(path)
    local_sha = gitops.short_sha(path)
    upstream_sha = gitops.short_sha(path, f"refs/remotes/{remote}/HEAD") if remote else None
    recent_sha = gitops.short_sha(path, f"HEAD~{window_days}")  # placeholder, replaced below
    try:
        recent_sha = gitops.short_sha(path, gitops.recent_ref(path, window_days))
    except Exception:
        recent_sha = None

    upstream_count = remote_ahead_count(path, remote) if remote else 0
    recent_count = gitops.recent_count(path, window_days)
    dirty = gitops.is_dirty(path)
    ahead, behind = gitops.ahead_behind(path, remote) if remote else (0, 0)
    dirty_pending = dirty and remote and upstream_count > 0
    working_tree = gitops.working_tree_state(path)
    try:
        last_commit = gitops.short_sha(path)
    except Exception:
        last_commit = None

    return RepoSnapshot(
        path=path, repo_name=name, upstream_url=remote and _remote_url(path, remote),
        base_ref=base_ref, local_sha=local_sha, upstream_sha=upstream_sha, recent_sha=recent_sha,
        upstream_count=upstream_count, recent_count=recent_count, dirty=dirty,
        dirty_with_upstream_pending=dirty_pending, working_tree=working_tree,
        ahead_by=ahead, behind_by=behind, upstream_branch=remote, last_commit=last_commit,
    )


def _remote_url(path: str, remote: str) -> str | None:
    try:
        return gitops._git(["remote", "get-url", remote], path)
    except Exception:
        return None


def recent_ref(path: str, window_days: int) -> str:
    """Return a ref N commits back where N = number of commits that occurred since
    the `window_days` boundary. Walk back from HEAD until a commit is older than
    the window, then return HEAD~N."""
    try:
        since = gitops._git(["log", "-1", "--pretty=%H", f"--since={window_days} days ago"], path)
    except Exception:
        return "HEAD"
    if not since:
        return "HEAD"
    # Walk back until we reach a commit older than the boundary.
    head = since.split()[0]
    n = 0
    cur = head
    while n < 1000:
        try:
            out = gitops._git(["log", "-1", "--pretty=%H", f"--date=iso", cur], path)
        except Exception:
            break
        if not out:
            break
        commit_date = out.split()[0]
        try:
            commit_dt = datetime.fromisoformat(commit_date.replace("Z", "+00:00"))
        except Exception:
            break
        if commit_dt < datetime.now(timezone.utc) - timedelta(days=window_days):
            return f"{head}~{n}"
        n += 1
        cur = out.split()[0]
    return f"{head}~{n}"


def detect_changes(snapshot: RepoSnapshot, window_days: int = RECENT_WINDOW_DAYS) -> list[str]:
    """Human-readable list of what changed: upstream commits and recent local activity."""
    lines: list[str] = []
    if snapshot.upstream_count:
        try:
            out = gitops._git(["log", "--oneline", f"HEAD..refs/remotes/{snapshot.upstream_branch}/HEAD"], snapshot.path)
            lines.append("Upstream new:\n" + "\n".join("  " + l for l in out.splitlines()[:8] if l) or "  (none)")
        except Exception:
            lines.append(f"Upstream new: {snapshot.upstream_count} commit(s)")
    if snapshot.recent_count:
        try:
            since = gitops._git(["log", f"--since={window_days} days ago", "--pretty=format:%h %s"], snapshot.path)
            commits = [l for l in since.splitlines() if l]
            lines.append("Recent local:\n" + "\n".join("  " + l for l in commits[:8]) or "  (none)")
        except Exception:
            lines.append(f"Recent local activity: {snapshot.recent_count} change(s)")
    if snapshot.dirty:
        try:
            status = gitops._git(["status", "--short"], snapshot.path)
            lines.append("Working tree changes:\n" + "\n".join("  " + l for l in status.splitlines() if l) or "  (untracked files)")
        except Exception:
            lines.append("Working tree changes (untracked/dirty)")
    return lines


def build_commit_message(snapshot: RepoSnapshot, repo_name: str) -> str:
    """Compose a commit message that captures upstream + recent changes."""
    lines = [f"chore: sync {repo_name}", ""]
    if snapshot.upstream_count:
        lines.append(f"Merge {snapshot.upstream_count} upstream commit(s)")
    if snapshot.recent_count:
        lines.append(f"Rebase {snapshot.recent_count} recent change(s)")
    if snapshot.dirty:
        lines.append("Include uncommitted working-tree changes")
    return "\n".join(lines)
