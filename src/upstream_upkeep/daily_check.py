"""Bring a focus branch up to date with an upstream, safely.

The daily check only ever fast-forwards on its own: no merge commit, nothing
of yours is rewritten, and only when the focus branch is checked out with a
clean working tree. Anything that needs a real merge (both sides moved, or
conflicts) becomes an approval request; approving it runs the merge here, and
a failed merge is aborted so the working tree is left as it was. Nothing is
ever pushed.
"""
from __future__ import annotations

from . import gitops


def blocker(path: str, focus: str) -> str | None:
    """Why the working tree can't be touched right now, or None if it can."""
    current = gitops.current_branch(path)
    if current != focus:
        return f"the checked-out branch is {current}, not the focus branch {focus}"
    if gitops.status_summary(path)["dirty"]:
        return "the working tree has uncommitted changes"
    return None


def merge_upstream(path: str, upstream: dict, focus: str, allow_merge_commit: bool = False) -> dict:
    """Merge refs/remotes/<name>/<branch> into `focus`. Returns {ok, action, detail}.

    Fast-forwards when the focus branch has no commits of its own. A merge
    commit is only made with allow_merge_commit (an approved request).
    """
    # "target" pins the merge to one upstream commit (e.g. the clean part of a pull report)
    ref = upstream.get("target") or f"refs/remotes/{upstream['name']}/{upstream.get('branch') or focus}"
    if not gitops.ref_exists(path, ref):
        return {"ok": False, "action": "none", "detail": f"{ref} not fetched"}
    behind, ahead = gitops.behind_ahead(path, focus, ref)
    if behind == 0:
        return {"ok": True, "action": "none", "detail": "already up to date"}
    reason = blocker(path, focus)
    if reason:
        return {"ok": False, "action": "none", "detail": reason}
    if ahead == 0:
        res = gitops._run(["git", "merge", "--ff-only", ref], cwd=path)
        if res.returncode != 0:
            return {"ok": False, "action": "none", "detail": (res.stderr or res.stdout).strip()}
        return {"ok": True, "action": "fast-forward", "detail": f"fast-forwarded {behind} commit(s) from {ref}"}
    if not allow_merge_commit:
        return {"ok": False, "action": "none", "detail": f"both sides moved ({behind} behind, {ahead} ahead): needs approval"}
    source = f"{upstream['name']}/{upstream.get('branch') or focus}"
    upto = f" up to {gitops.short_sha(path, ref)[:10]}" if upstream.get("target") else ""
    message = f"Merge {source}{upto} into {focus} ({behind} upstream commit(s))"
    res = gitops._run(["git", "merge", "--no-ff", "-m", message, ref], cwd=path)
    if res.returncode != 0:
        gitops._run(["git", "merge", "--abort"], cwd=path)
        return {"ok": False, "action": "aborted", "detail": "merge failed and was aborted: " + ((res.stdout or res.stderr).strip()[-500:])}
    return {"ok": True, "action": "merge", "detail": f"merged {behind} commit(s) from {ref}"}
