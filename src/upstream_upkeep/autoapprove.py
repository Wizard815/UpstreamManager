"""Auto-approval rules: requests that approve themselves when they pass your checks.

Per repo, the user (never an agent) picks which kinds of request may go in without asking:

  clean       a clean upstream pull (no conflicts) raised by the daily check or by an agent
  components  a pin bump of submodules to their upstream
  workspaces  a merge or change workspace an agent submitted (needs a check command)

and, optionally, a check command (``npm ci && npm test``, ``make test``...). When such a request is
raised, the exact result it would produce is rebuilt in a throwaway checkout next to the state file
(the focus branch as it is now with the upstream commits or the workspace merged in, or the new pins
checked out), the command runs
there, and only when it exits 0 is the request approved, as if you had pressed Approve. A failing
check leaves the request waiting for you, with the end of the output on it. Pushes and Actions
changes are never approved this way. Nothing is ever pushed.
"""
from __future__ import annotations

import secrets
import shutil
import subprocess
import threading
from pathlib import Path

from . import config, store

KINDS = ("clean", "components", "workspaces")
DEFAULT_TIMEOUT = 1200
_approve = None          # set by app.py: app.decide(req, decision, comment) -> (request, failed)
_threads: list[threading.Thread] = []
_repo_locks: dict[str, threading.Lock] = {}


def rules(repo: dict) -> dict:
    r = dict(repo.get("auto_approve") or {})
    return {"clean": bool(r.get("clean")), "components": bool(r.get("components")),
            "workspaces": bool(r.get("workspaces")), "check": (r.get("check") or "").strip(),
            "timeout": int(r.get("timeout") or DEFAULT_TIMEOUT)}


def clean_rules(data: dict) -> dict:
    """Validate what the page sends."""
    out = {k: bool(data.get(k)) for k in KINDS}
    check = data.get("check") or ""
    if not isinstance(check, str) or len(check) > 2000 or "\n\n\n" in check:
        raise ValueError("the check command is one command line (or a few), up to 2000 characters")
    out["check"] = check.strip()
    try:
        out["timeout"] = max(30, min(int(data.get("timeout") or DEFAULT_TIMEOUT), 6 * 3600))
    except (TypeError, ValueError):
        raise ValueError("timeout is a number of seconds")
    if out["workspaces"] and not out["check"]:
        raise ValueError("an agent's workspace only goes in by itself when a check command passes: set one")
    return out


def kind_of(req: dict) -> str | None:
    if req.get("type") == "components":
        return "components"
    if req.get("type"):          # push, actions: never by themselves
        return None
    if req.get("workspace"):
        return "workspaces"
    if req.get("safe") and not req.get("conflicts") and not any(u.get("conflict") for u in req.get("upstreams") or []):
        return "clean"
    return None


def _git(cwd, *args, timeout=300):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)


_ID = ["-c", "user.name=Upstream Manager", "-c", "user.email=upstream-manager@localhost"]


def _prepare(repo: dict, req: dict, kind: str, where: Path) -> str | None:
    """Rebuild in `where` what approving `req` would produce. Returns why it can't, or None."""
    path, focus = repo["path"], req.get("focus_branch") or repo.get("focus_branch") or "main"
    # Always the focus branch as it is now, with the request applied on top: approving merges into the
    # focus branch of today, which may have moved since the request (or the workspace) was made.
    res = _git(path, "worktree", "add", "--detach", str(where), f"refs/heads/{focus}")
    if res.returncode != 0:
        return (res.stderr or res.stdout).strip()[-300:] or "could not make a checkout"
    if kind in ("clean", "workspaces"):
        for u in req.get("upstreams") or []:
            target = u.get("target") or u.get("upstream_sha")
            if not target:
                return f"{u.get('name')}: no commit to merge"
            m = _git(where, *_ID, "merge", "--no-ff", "--no-edit", "--end-of-options", target)
            if m.returncode != 0:
                return f"{u.get('name')} does not merge cleanly into {focus} any more"
    if (where / ".gitmodules").is_file():
        init = _git(where, "submodule", "update", "--init", "--recursive", timeout=1800)
        if init.returncode != 0:
            return "could not check out the submodules: " + (init.stderr or init.stdout).strip()[-300:]
    if kind == "components":
        for c in req.get("components") or []:
            target = c.get("upstream")
            sub = where / c.get("path", "")
            if not target or not sub.is_dir():
                return f"{c.get('name')}: nothing to check out"
            if _git(sub, "checkout", "-q", "--detach", target).returncode != 0:
                fetched = _git(sub, "fetch", "-q", "origin", target, timeout=900)
                if fetched.returncode != 0 or _git(sub, "checkout", "-q", "--detach", target).returncode != 0:
                    return f"{c.get('name')}: could not check out {target[:10]}"
    return None


def run_check(repo: dict, req: dict, kind: str, r: dict) -> tuple[bool, str]:
    """(passed, what happened)."""
    where = config.CONFIG_PATH.parent / "checks" / repo["id"] / secrets.token_hex(4)
    where.parent.mkdir(parents=True, exist_ok=True)
    try:
        why = _prepare(repo, req, kind, where)
        if why:
            return False, why
        if not r["check"]:
            return True, "no check command set: it merges cleanly"
        try:
            res = subprocess.run(["bash", "-lc", r["check"]], cwd=str(where), capture_output=True, text=True,
                                 timeout=r["timeout"])
        except subprocess.TimeoutExpired:
            return False, f"`{r['check']}` took longer than {r['timeout']}s"
        out = ((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip()[-4000:]
        return res.returncode == 0, f"`{r['check']}` exited {res.returncode}\n{out}"
    finally:
        _git(repo["path"], "worktree", "remove", "--force", str(where))
        shutil.rmtree(where, ignore_errors=True)
        _git(repo["path"], "worktree", "prune")


def consider(req_id: str) -> dict | None:
    """Apply the repo's rules to one pending request (blocking). Returns what was done, or None."""
    from . import registry
    req = store.snapshot(store.get_request(req_id))
    if not req or req.get("status") != "pending":
        return None
    repo = registry.get_repo(req["repo_id"])
    if not repo:
        return None
    r, kind = rules(repo), kind_of(req)
    if not kind or not r.get(kind):
        return None
    if kind == "workspaces" and not r["check"]:
        return None
    lock = _repo_locks.setdefault(repo["id"], threading.Lock())
    with lock:
        store.annotate_request(req_id, auto={"state": "checking", "kind": kind, "check": r["check"], "at": config.now_iso()})
        try:
            ok, detail = run_check(repo, req, kind, r)
        except Exception as e:  # never leave it saying "checking"
            ok, detail = False, f"the check could not run: {e}"
        now = store.get_request(req_id)
        if not now or now.get("status") != "pending" or now.get("created_at") != req.get("created_at"):
            return None   # decided by hand, or replaced by a newer request, meanwhile
        if not ok:
            from . import notify
            notify.notify("auto_failed", f"{repo.get('name') or repo['id']}: check failed, it waits for you",
                          f"{kind} request {req_id}: " + detail.strip().splitlines()[0][:300] if detail.strip() else kind,
                          level="high", path=f"/requests#req-{req_id}", key=f"auto-failed:{req_id}:{req.get('created_at')}", repo=repo["id"])
            return store.annotate_request(req_id, auto={"state": "failed", "kind": kind, "check": r["check"],
                                                         "detail": detail, "at": config.now_iso()})
        store.annotate_request(req_id, auto={"state": "passed", "kind": kind, "check": r["check"], "detail": detail,
                                             "at": config.now_iso()})
        result, failed = _approve(now, "approve", f"Approved by the auto-approval rule for {kind}: "
                                  + (f"`{r['check']}` passed" if r["check"] else "it merges cleanly"))
    from . import notify
    notify.notify("auto_approved" if not failed else "auto_failed",
                  f"{repo.get('name') or repo['id']}: {kind} request " + ("went in by itself" if not failed else "passed its check but failed to merge"),
                  req_id, level="low" if not failed else "high", path="/requests", repo=repo["id"])
    if not failed:
        # The repo moved on: a request whose check failed before may pass on top of this one.
        for other in store.snapshot(store.get_requests()):
            if (other.get("repo_id") == repo["id"] and other.get("status") == "pending"
                    and (other.get("auto") or {}).get("state") == "failed"):
                _on_request(other)
    return result


def handles(rec: dict) -> bool:
    """Will an auto-approval rule look at this request (so the user hears about the outcome instead)?"""
    from . import registry
    kind = kind_of(rec)
    repo = registry.get_repo(rec.get("repo_id") or "")
    if not kind or not repo:
        return False
    r = rules(repo)
    return bool(r.get(kind)) and (kind != "workspaces" or bool(r["check"]))


def _on_request(rec: dict) -> None:
    if _approve is None or not kind_of(rec):
        return
    t = threading.Thread(target=consider, args=(rec["id"],), daemon=True, name=f"auto-approve {rec['id']}")
    _threads[:] = [x for x in _threads if x.is_alive()] + [t]
    t.start()


def wait(timeout: float = 600) -> None:
    """Wait for the checks started so far (tests)."""
    for t in list(_threads):
        t.join(timeout)


def install(approve) -> None:
    global _approve
    _approve = approve
    if _on_request not in store.REQUEST_HOOKS:
        store.REQUEST_HOOKS.append(_on_request)


def describe(repo: dict) -> str:
    """One line for the agent briefing."""
    r = rules(repo)
    on = [k for k in KINDS if r[k]]
    if not on:
        return "Auto-approval: off (every request waits for the user)."
    return (f"Auto-approval (set by the user, not changeable by agents): {', '.join(on)} requests go in by themselves"
            + (f" when `{r['check']}` passes in a fresh checkout of the result." if r["check"] else " when they merge cleanly."))

