"""Pull report: which incoming upstream commits are a no-issue pull and which conflict.

For `mine` (your branch, or a submodule's pin) and `theirs` (the fetched
upstream), everything here is read-only, built on `git merge-tree`:

  conflict files  files that conflict when merging ALL of theirs into mine
  conflicting     incoming commits that touch at least one conflict file
  no issue        incoming commits that touch none of them
  watched         incoming commits that touch watched paths (follow-up work,
                  e.g. files a hub copies), counted separately
  clean prefix    the newest point on the upstream's first-parent line that
                  merges into mine with zero conflicts: taking it brings in
                  every commit up to there safely. Found by binary search
                  (about log2(N) merge simulations).

Why not cherry-pick the no-issue commits one by one past a conflict? They
often depend on the conflicting ones, and the skipped commits would conflict
again on every later merge. The clean prefix is the safe unit to pull.
"""
from __future__ import annotations

from . import gitops

SAMPLE = 15  # conflicting commits listed by name


def _git(path: str, *args: str) -> str | None:
    res = gitops._run(["git", *args], cwd=path)
    return res.stdout if res.returncode == 0 else None


def _commit_files(path: str, rng: str) -> list[dict]:
    """Every commit in the range, oldest first, with the files it touches."""
    out = _git(path, "log", "--reverse", "--no-renames", "--name-only", "--format=%x1e%H%x1f%h%x1f%P%x1f%s", rng) or ""
    commits = []
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        head, *files = block.strip("\n").split("\n")
        full, sha, parents, subject = head.split("\x1f")
        commits.append({"full": full, "sha": sha, "subject": subject, "merge": len(parents.split()) > 1,
                        "files": [f for f in files if f.strip()]})
    return commits


def _merges_clean(path: str, mine: str, theirs: str) -> bool | None:
    res = gitops.merge_check(path, mine, theirs)
    return None if not res["ok"] else not res["conflict"]


def clean_prefix(path: str, mine: str, theirs: str) -> dict:
    """Newest first-parent commit of `theirs` that merges into `mine` without conflict."""
    chain = (_git(path, "rev-list", "--first-parent", "--reverse", f"{mine}..{theirs}") or "").split()
    if not chain:
        return {"sha": None, "commits": 0, "of": 0, "complete": True}
    if _merges_clean(path, mine, chain[-1]):
        lo = len(chain) - 1
    else:
        lo, hi = -1, len(chain) - 1  # invariant: chain[hi] conflicts; chain[lo] is clean (or none)
        while hi - lo > 1:
            mid = (lo + hi) // 2
            if _merges_clean(path, mine, chain[mid]):
                lo = mid
            else:
                hi = mid
    if lo < 0:
        return {"sha": None, "commits": 0, "of": len(chain), "complete": False,
                "first_conflict": gitops.short_sha(path, chain[0])}
    sha = chain[lo]
    count = int((_git(path, "rev-list", "--count", f"{mine}..{sha}") or "0").strip() or 0)
    return {"sha": sha, "short": sha[:10], "commits": count, "of": len(chain), "complete": lo == len(chain) - 1,
            "first_conflict": None if lo == len(chain) - 1 else chain[lo + 1][:10]}


def analyze(path: str, mine: str, theirs: str, watch_paths: list[str] | None = None) -> dict:
    """The full report for pulling `theirs` into `mine` (both commit-ish, in repo `path`)."""
    rng = f"{mine}..{theirs}"
    commits = _commit_files(path, rng)
    full = gitops.merge_check(path, mine, theirs)
    if not full["ok"]:
        return {"ok": False, "error": full.get("error") or "merge simulation failed"}
    conflict_files = set(full["files"])
    watched_set: set[str] = set()
    if watch_paths:
        names = _git(path, "diff", "--name-only", f"{mine}...{theirs}", "--", *watch_paths) or ""
        watched_set = {n for n in names.splitlines() if n.strip()}
    plain = [c for c in commits if not c["merge"]]
    conflicting = [c for c in plain if conflict_files.intersection(c["files"])]
    watched = [c for c in plain if watched_set.intersection(c["files"])]
    behind, ahead = gitops.behind_ahead(path, mine, theirs)
    report = {
        "ok": True,
        "incoming": behind,                 # every commit theirs has that mine lacks
        "merge_commits": len(commits) - len(plain),
        "changes": len(plain),             # the commits that carry changes
        "no_issue": len(plain) - len(conflicting),
        "conflicting": len(conflicting),
        "watched": len(watched),
        "yours": ahead,
        "fast_forward": ahead == 0,
        "conflict_files": sorted(conflict_files),
        "conflicting_sample": [{"sha": c["sha"], "subject": c["subject"],
                                "files": sorted(conflict_files.intersection(c["files"]))[:5]} for c in conflicting[:SAMPLE]],
        "watched_sample": [{"sha": c["sha"], "subject": c["subject"],
                            "files": sorted(watched_set.intersection(c["files"]))[:5]} for c in watched[:SAMPLE]],
    }
    # With no commits of yours the whole range fast-forwards: nothing can conflict.
    report["clean_prefix"] = ({"sha": gitops.short_sha(path, theirs), "commits": behind, "of": behind, "complete": True}
                              if ahead == 0 or not conflict_files else clean_prefix(path, mine, theirs))
    return report
