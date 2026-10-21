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
    conflict_files: list[str] = field(default_factory=list)
    missing_ref: bool = False

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
            "conflict_files": self.conflict_files,
            "missing_ref": self.missing_ref,
        }


def _remote_url(path: str, remote: str) -> str | None:
    return gitops.remotes(path).get(remote)


_merge_cache: dict[tuple[str, str], dict] = {}


def cached_merge_check(path: str, local_sha: str, upstream_sha: str) -> dict:
    """merge_check keyed by the two commit ids, so unchanged refs are never re-merged."""
    key = (local_sha, upstream_sha)
    if key not in _merge_cache:
        if len(_merge_cache) > 200:
            _merge_cache.clear()
        _merge_cache[key] = gitops.merge_check(path, local_sha, upstream_sha)
    return _merge_cache[key]


def compare_upstream(path: str, upstream: dict, focus_branch: str | None = None,
                     fetch: bool = False) -> UpstreamComparison:
    """Compare the focus branch against the upstream's branch.

    `behind` = commits the upstream has that the focus branch lacks; `ahead` =
    commits only the focus branch has. Network access only happens with
    fetch=True (the Fetch button, the daily check); everything else reads the
    refs already in the repo. Conflicts come from a merge simulation that does
    not touch the working tree.
    """
    remote = upstream["name"]
    url = upstream.get("url") or _remote_url(path, remote) or remote
    focus = focus_branch or gitops.current_branch(path) or "main"
    branch = upstream.get("branch") or focus

    fetch_error = None
    if fetch:
        ok, msg = gitops.fetch(path, remote)
        if not ok:
            fetch_error = msg

    ref = f"refs/remotes/{remote}/{branch}"
    if not gitops.ref_exists(path, ref):
        return UpstreamComparison(name=remote, url=url, branch=branch, ahead=0, behind=0, up_to_date=False,
                                  conflict_detail=fetch_error or f"{remote}/{branch} not fetched yet - press Fetch",
                                  missing_ref=True)
    local = gitops.short_sha(path, focus)
    upstream_sha = gitops.short_sha(path, ref)
    behind, ahead = gitops.behind_ahead(path, focus, ref)

    conflict, files, detail = False, [], fetch_error
    if behind and ahead and local and upstream_sha:
        # both sides moved: simulate the merge (read-only)
        check = cached_merge_check(path, local, upstream_sha)
        conflict, files = check["conflict"], check["files"]
        if not check["ok"]:
            detail = check.get("error")
        elif conflict:
            detail = f"{len(files)} conflicting file(s)"

    return UpstreamComparison(
        name=remote, url=url, branch=branch, ahead=ahead, behind=behind,
        up_to_date=(ahead == 0 and behind == 0), local_sha=local, upstream_sha=upstream_sha,
        conflict=conflict, conflict_detail=detail, conflict_files=files,
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
                     focus_branch: str | None = None, fetch: bool = False) -> MergePlan:
    """Compare each upstream to the focus branch and decide if a safe merge is possible."""
    focus = focus_branch or gitops.current_branch(path) or "main"
    comparisons: list[UpstreamComparison] = []
    conflicts: list[str] = []
    actions: list[str] = []

    for up in upstreams:
        if not up.get("enabled", True):
            continue
        comp = compare_upstream(path, up, focus, fetch=fetch)
        comparisons.append(comp)
        if comp.missing_ref:
            actions.append(f"FETCH {up['name']}/{up.get('branch', focus)} first")
        elif comp.conflict:
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
