"""Upstream comparison + merge planning for a repo's focus branch.

For each upstream we compare the repo's focus branch to the upstream's branch
and compute ahead/behind counts plus a merge plan. The daily cron job runs this
for every watched repo, decides whether the merge is safe (no conflicts), and
either auto-merges+pushes or raises a request for approval.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from . import gitops
from .config import now_iso


@dataclass
class UpstreamComparison:
    name: str
    url: str
    branch: str
    ahead: int
    behind: int
    up_to_date: bool
    local_sha: str | None = None
    upstream_sha: str | None = None
    conflict: bool = False
    conflict_detail: str | None = None
    refspec: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "url": self.url,
            "branch": self.branch,
            "ahead": self.ahead,
            "behind": self.behind,
            "up_to_date": self.up_to_date,
            "local_sha": self.local_sha,
            "upstream_sha": self.upstream_sha,
            "conflict": self.conflict,
            "conflict_detail": self.conflict_detail,
            "refspec": self.refspec,
        }


def _fetch_remote(path: str, remote: str) -> bool:
    res = gitops._run(["git", "fetch", remote], cwd=path, check=False)
    if res.returncode != 0:
        # remote may not exist locally; add it
        url = _remote_url(path, remote)
        if url:
            gitops._run(["git", "remote", "add", remote, url], cwd=path)
            gitops._run(["git", "fetch", remote], cwd=path)
    return res.returncode == 0


def _remote_url(path: str, remote: str) -> str | None:
    remotes = gitops.remotes(path)
    for k, v in remotes.items():
        if k == remote:
            return v
    return None


def compare_upstream(path: str, upstream: dict, focus_branch: str | None = None) -> UpstreamComparison:
    """Compare focus branch (default current) against the upstream's branch."""
    remote = upstream["name"]
    url = upstream.get("url") or _remote_url(path, remote) or remote
    branch = upstream.get("branch") or (focus_branch or gitops.current_branch(path) or "main")

    _fetch_remote(path, remote)

    # ensure the remote-tracking branch exists for the upstream's own branch
    # fetch all remote branches into refs/remotes/<name>/* (works even if the
    # specific branch name isn't resolvable as a refspec target)
    gitops._git(["fetch", "-f", remote, f"+refs/heads/*:refs/remotes/{remote}/*"], path)
    ref = f"refs/remotes/{remote}/{branch}"
    local = gitops.short_sha(path, focus_branch or gitops.current_branch(path) or "main")
    upstream_sha = gitops.short_sha(path, ref)

    ahead = 0
    behind = 0
    try:
        ahead = int(gitops._git(["rev-list", "--count", f"{local}..{upstream_sha}"], path) or 0)
    except Exception:
        ahead = 0
    try:
        behind = int(gitops._git(["rev-list", "--count", f"{upstream_sha}..{local}"], path) or 0)
    except Exception:
        behind = 0

    conflict = False
    conflict_detail = None
    if ahead and behind and upstream_sha:
        # both sides moved — merge will likely need resolution
        res = gitops._run(["git", "merge", "--no-commit", "--no-ff", upstream_sha], cwd=path)
        if res.returncode != 0:
            conflict = True
            conflict_detail = res.stderr.strip() or res.stdout.strip()
        else:
            gitops._run(["git", "merge", "--abort"], cwd=path)

    return UpstreamComparison(
        name=remote,
        url=url,
        branch=branch,
        ahead=ahead,
        behind=behind,
        up_to_date=(ahead == 0 and behind == 0),
        local_sha=local,
        upstream_sha=upstream_sha,
        conflict=conflict,
        conflict_detail=conflict_detail,
    )


@dataclass
class MergePlan:
    repo_id: str
    repo_name: str
    repo_path: str
    focus_branch: str
    upstreams: list["UpstreamComparison"]
    conflicts: list[str]
    safe: bool
    actions: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_id": self.repo_id,
            "repo_name": self.repo_name,
            "repo_path": self.repo_path,
            "focus_branch": self.focus_branch,
            "upstreams": [u.to_dict() for u in self.upstreams],
            "conflicts": self.conflicts,
            "safe": self.safe,
            "actions": self.actions,
        }


def build_merge_plan(path: str, repo_id: str, name: str, upstreams: list[dict],
                     focus_branch: str | None = None) -> MergePlan:
    """Compare each upstream to the focus branch and decide if a safe merge is possible."""
    focus = focus_branch or gitops.current_branch(path) or "main"
    comparisons: list[UpstreamComparison] = []
    conflicts: list[str] = []
    actions: list[str] = []

    for up in upstreams:
        if not up.get("enabled", True):
            continue
        comp = compare_upstream(path, up, focus)
        comparisons.append(comp)
        if comp.conflict:
            conflicts.append(f"{up['name']}/{up.get('branch', focus)}")
            actions.append(f"SKIP merge {up['name']}/{up.get('branch', focus)}: conflict")
        elif comp.ahead or comp.behind:
            actions.append(f"MERGE {up['name']}/{up.get('branch', focus)}: +{comp.behind} / -{comp.ahead}")
        else:
            actions.append(f"OK {up['name']}/{up.get('branch', focus)}: up to date")

    safe = not conflicts
    return MergePlan(
        repo_id=repo_id,
        repo_name=name,
        repo_path=path,
        focus_branch=focus,
        upstreams=comparisons,
        conflicts=conflicts,
        safe=safe,
        actions=actions,
    )
