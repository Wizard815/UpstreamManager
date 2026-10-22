"""Components: git submodules tracked as parts of one repo.

A hub repo (e.g. ALLGlance) pins other repos as submodules. Each submodule is a
*component*: it keeps its own upstream and is fetched and compared on its own,
inside its own git directory, so the hub never downloads the other projects'
history. The hub still behaves as one unit: one card shows every component's
drift, and one approval moves all the pins forward in a single commit.

For each component:
  pin       the commit the hub's HEAD records for that path
  upstream  refs/remotes/origin/<branch> inside the submodule (after a fetch)
  behind    upstream commits newer than the pin
  local     commits in the pin that the upstream does not have (your own work)
  watched   paths whose upstream changes need follow-up work in the hub (e.g.
            files the hub copies), shown with a hint command; never run

Config the user edits per component (stored on the repo): enabled, branch,
watch_paths, after_update. Everything else is re-read from git every time.
"""
from __future__ import annotations

from pathlib import Path

from . import gitops

_EDITABLE = ("enabled", "branch", "watch_paths", "after_update")


def _git(cwd, *args):
    return gitops._run(["git", *args], cwd=str(cwd))


def _out(cwd, *args) -> str | None:
    res = _git(cwd, *args)
    return res.stdout.strip() if res.returncode == 0 else None


def detect(repo_path: str) -> list[dict]:
    """Submodules declared in .gitmodules: [{name, path, url, branch}]."""
    if not (Path(repo_path) / ".gitmodules").is_file():
        return []
    raw = _out(repo_path, "config", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url|branch)$") or ""
    found: dict[str, dict] = {}
    for line in raw.splitlines():
        key, _, value = line.partition(" ")
        name, field = key[len("submodule."):].rsplit(".", 1)
        found.setdefault(name, {"name": name})[field] = value.strip()
    return [c for c in found.values() if c.get("path") and c.get("url")]


def sync(repo: dict) -> list[dict]:
    """Merge .gitmodules into repo["components"], keeping the user's settings.

    Components removed from .gitmodules disappear. Returns the new list (the
    caller saves the repo).
    """
    old = {c["name"]: c for c in repo.get("components") or []}
    merged = []
    for c in detect(repo["path"]):
        prev = old.get(c["name"], {})
        merged.append({
            "name": c["name"], "path": c["path"], "url": c["url"],
            "enabled": prev.get("enabled", True),
            "branch": prev.get("branch") or c.get("branch") or "",
            "watch_paths": prev.get("watch_paths") or [],
            "after_update": prev.get("after_update") or "",
        })
    return merged


def update_settings(comp: dict, patch: dict) -> dict:
    for k in _EDITABLE:
        if k not in patch:
            continue
        v = patch[k]
        if k == "watch_paths":
            v = [p.strip().strip("/") for p in (v if isinstance(v, list) else str(v).split(",")) if p.strip()]
        elif k == "enabled":
            v = bool(v)
        else:
            v = str(v or "").strip()
        comp[k] = v
    return comp


def _sub(repo_path: str, comp: dict) -> Path:
    return Path(repo_path) / comp["path"]


def initialized(repo_path: str, comp: dict) -> bool:
    return (_sub(repo_path, comp) / ".git").exists()


def pin(repo_path: str, comp: dict) -> str | None:
    """The commit the hub's HEAD pins for this path (a gitlink entry)."""
    out = _out(repo_path, "ls-tree", "HEAD", "--", comp["path"])
    if not out:
        return None
    mode, kind, sha = out.split()[:3]
    return sha if kind == "commit" else None


def branch(repo_path: str, comp: dict) -> str:
    """Configured branch, else the upstream's default (origin/HEAD), else main/master."""
    if comp.get("branch"):
        return comp["branch"]
    sub = _sub(repo_path, comp)
    head = _out(sub, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        return head.split("/", 1)[1]
    for b in ("main", "master"):
        if gitops.ref_exists(str(sub), f"refs/remotes/origin/{b}"):
            return b
    return "main"


def fetch(repo_path: str, comp: dict) -> tuple[bool, str]:
    """Network step: check the submodule out if needed, then fetch its upstream.

    A first checkout uses a blob-less partial clone, so large projects (vikunja)
    download file contents only when they are actually needed.
    """
    if not initialized(repo_path, comp):
        res = _git(repo_path, "submodule", "update", "--init", "--filter=blob:none", "--", comp["path"])
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
    sub = _sub(repo_path, comp)
    res = _git(sub, "fetch", "--prune", "origin")
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()
    if not comp.get("branch"):
        _git(sub, "remote", "set-head", "origin", "--auto")
    return True, ""


def compare(repo_path: str, comp: dict, do_fetch: bool = False) -> dict:
    """State of one component, from what is already fetched unless do_fetch."""
    out = {"name": comp["name"], "path": comp["path"], "url": comp["url"], "enabled": comp.get("enabled", True),
           "watch_paths": comp.get("watch_paths") or [], "after_update": comp.get("after_update") or "",
           "pin": pin(repo_path, comp), "status": "ok", "detail": None, "behind": 0, "local": 0,
           "watched_changes": [], "missing": [], "conflict": False, "conflict_files": []}
    if do_fetch:
        ok, msg = fetch(repo_path, comp)
        if not ok:
            out.update(status="error", detail=f"fetch failed: {msg}")
            return out
    if not initialized(repo_path, comp):
        out.update(status="not_checked_out", detail="not checked out yet - press Fetch")
        return out
    sub = str(_sub(repo_path, comp))
    out["branch"] = br = branch(repo_path, comp)
    ref = f"refs/remotes/origin/{br}"
    checked_out = _out(sub, "rev-parse", "HEAD")
    out["checked_out"] = checked_out
    out["moved"] = bool(checked_out and out["pin"] and checked_out != out["pin"])
    out["dirty"] = len([l for l in (_out(sub, "status", "--porcelain") or "").splitlines() if l.strip()])
    if not out["pin"]:
        out.update(status="error", detail="the hub's HEAD has no pin for this path")
        return out
    if not gitops.ref_exists(sub, ref) or not gitops.ref_exists(sub, out["pin"]):
        out.update(status="not_fetched", detail=f"origin/{br} not fetched yet - press Fetch")
        return out
    out["upstream"] = gitops.short_sha(sub, ref)
    out["behind"], out["local"] = gitops.behind_ahead(sub, out["pin"], ref)
    if out["behind"]:
        # Real changes, not "Merge pull request ..." lines, which say little.
        out["missing"] = gitops.list_commits(sub, f"{out['pin']}..{ref}", 0, 5, no_merges=True)
        if out["watch_paths"]:
            names = _out(sub, "diff", "--name-only", out["pin"], ref, "--", *out["watch_paths"]) or ""
            out["watched_changes"] = [n for n in names.splitlines() if n.strip()]
    if out["behind"] and out["local"]:
        check = gitops.merge_check(sub, out["pin"], ref)
        out["conflict"], out["conflict_files"] = check["conflict"], check["files"]
    if out["behind"] == 0 and out["local"] == 0:
        out["status"] = "up_to_date"
    return out


def compare_all(repo: dict, do_fetch: bool = False) -> list[dict]:
    return [compare(repo["path"], c, do_fetch) if c.get("enabled", True) else
            {"name": c["name"], "path": c["path"], "url": c["url"], "enabled": False, "status": "disabled",
             "behind": 0, "local": 0, "watched_changes": [], "missing": []}
            for c in repo.get("components") or []]


def blocker(repo_path: str, focus: str, comp_paths: list[str]) -> str | None:
    """Why the hub can't take a pin-bump commit right now, or None."""
    current = gitops.current_branch(repo_path)
    if current != focus:
        return f"the checked-out branch is {current}, not the focus branch {focus}"
    staged = [p for p in (_out(repo_path, "diff", "--cached", "--name-only") or "").splitlines() if p.strip()]
    others = [p for p in staged if p not in comp_paths]
    if others:
        return f"other changes are already staged ({', '.join(others[:5])}); commit or unstage them first"
    return None


def bump(repo: dict, names: list[str], focus: str) -> dict:
    """Move the named components' pins to their fetched upstream in ONE hub commit.

    Fast-forward only: a component whose pin has commits the upstream lacks, a
    dirty submodule, or a checkout that differs from the pin is skipped with the
    reason. If the commit fails, every submodule is put back on its old pin.
    Nothing is pushed.
    """
    path = repo["path"]
    comps = {c["name"]: c for c in repo.get("components") or []}
    results, moved = [], []
    reason = blocker(path, focus, [comps[n]["path"] for n in names if n in comps])
    if reason:
        return {"ok": False, "detail": reason, "results": [], "commit": None}
    for name in names:
        comp = comps.get(name)
        if not comp:
            results.append({"name": name, "ok": False, "detail": "no such component"})
            continue
        st = compare(path, comp)
        sub = str(_sub(path, comp))
        if st["status"] in ("not_checked_out", "not_fetched", "error"):
            results.append({"name": name, "ok": False, "detail": st["detail"]})
        elif st["behind"] == 0:
            results.append({"name": name, "ok": True, "detail": "already up to date", "skipped": True})
        elif st["local"]:
            results.append({"name": name, "ok": False, "detail": f"the pin has {st['local']} commit(s) that are not upstream; "
                            "merge inside the submodule first"})
        elif st["dirty"]:
            results.append({"name": name, "ok": False, "detail": "the submodule has uncommitted changes"})
        elif st["moved"]:
            results.append({"name": name, "ok": False, "detail": "the submodule is checked out at a different commit than the pin"})
        else:
            target = gitops.short_sha(sub, f"refs/remotes/origin/{st['branch']}")
            res = _git(sub, "checkout", "-q", "--detach", target)
            if res.returncode != 0:
                results.append({"name": name, "ok": False, "detail": (res.stderr or res.stdout).strip()})
                continue
            moved.append((comp, st["pin"], target, st["behind"]))
            results.append({"name": name, "ok": True, "from": st["pin"][:10], "to": target[:10], "behind": st["behind"],
                            "detail": f"{st['pin'][:8]} -> {target[:8]} ({st['behind']} commits)",
                            "watched_changes": st["watched_changes"],
                            "after_update": comp.get("after_update") if st["watched_changes"] else ""})
    if not moved:
        return {"ok": all(r["ok"] for r in results), "detail": "nothing to bump", "results": results, "commit": None}

    paths = [c["path"] for c, *_ in moved]
    subject = "Bump " + ", ".join(f"{c['name']} to {t[:8]}" for c, _, t, _ in moved)
    body = "\n".join(f"- {c['name']} ({c['path']}): {p[:10]} -> {t[:10]}, {n} upstream commit(s)" for c, p, t, n in moved)
    res = _git(path, "add", "--", *paths)
    if res.returncode == 0:
        res = _git(path, "commit", "-q", "-m", subject, "-m", body, "--", *paths)
    if res.returncode != 0:
        for comp, old_pin, *_ in moved:  # leave everything as it was
            _git(_sub(path, comp), "checkout", "-q", "--detach", old_pin)
        _git(path, "reset", "-q", "--", *paths)
        detail = (res.stderr or res.stdout).strip()
        if "Please tell me who you are" in detail or "user.email" in detail:
            detail = "git has no author identity here: set user.name and user.email, then approve again"
        for r in results:
            if r.get("from"):
                r.update(ok=False, detail=f"rolled back: {detail}")
        return {"ok": False, "detail": f"commit failed and was rolled back: {detail}", "results": results, "commit": None}
    commit = gitops.short_sha(path)
    failed = [r for r in results if not r["ok"]]
    return {"ok": not failed, "detail": subject + (f"; {len(failed)} component(s) skipped" if failed else ""),
            "results": results, "commit": commit}
