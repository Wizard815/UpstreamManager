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

import re
import subprocess

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import gitops

# Without a tag_pattern, a release is a plain version (v2.8.0, 3.6.0): not "vue3", not v1.0.0-rc.1.
_RELEASE = re.compile(r"^v?\d+(\.\d+){1,3}$")
_EDITABLE = ("enabled", "branch", "watch_paths", "after_update", "follow", "tag_pattern", "min_age_days")


def _git(cwd, *args):
    return gitops._run(["git", *args], cwd=str(cwd))


def _out(cwd, *args) -> str | None:
    res = _git(cwd, *args)
    return res.stdout.strip() if res.returncode == 0 else None


def detect(repo_path: str) -> list[dict]:
    """Submodules declared in .gitmodules: [{name, path, url, branch}]."""
    if not (Path(repo_path) / ".gitmodules").is_file():
        return []
    # -z: each entry is "key\nvalue\0", so names with spaces split correctly.
    res = _git(repo_path, "config", "-z", "-f", ".gitmodules", "--get-regexp", r"^submodule\..*\.(path|url|branch)$")
    raw = res.stdout if res.returncode == 0 else ""
    found: dict[str, dict] = {}
    for entry in filter(None, raw.split("\0")):
        key, _, value = entry.partition("\n")
        name, _, field = key[len("submodule."):].rpartition(".")
        if name and field:
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
            "follow": prev.get("follow") or "branch",
            "tag_pattern": prev.get("tag_pattern") or "",
            "min_age_days": prev.get("min_age_days") or 0,
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
        elif k == "follow":
            v = "releases" if v == "releases" else "branch"
        elif k == "min_age_days":
            try:
                v = max(0.0, min(float(v or 0), 365.0))
            except (TypeError, ValueError):
                v = 0.0
        else:
            v = str(v or "").strip()
        comp[k] = v
    return comp


def _sub(repo_path: str, comp: dict) -> Path:
    return Path(repo_path) / comp["path"]


def initialized(repo_path: str, comp: dict) -> bool:
    """Checked out as a submodule (its files are on disk)."""
    return (_sub(repo_path, comp) / ".git").exists()


def _mirror(repo_path: str, comp: dict) -> Path:
    """Where a component that is not checked out keeps its upstream's history, for comparing only:
    a bare, tree-less copy (commits and tags, no folders or files) inside the hub's git dir."""
    common = (_out(repo_path, "rev-parse", "--path-format=absolute", "--git-common-dir") or str(Path(repo_path) / ".git"))
    return Path(common) / "upkeep-components" / (re.sub(r"[^A-Za-z0-9._-]+", "_", comp["name"]) + ".git")


def store_dir(repo_path: str, comp: dict) -> Path | None:
    """The git dir to read a component's history from: its checkout, else its history copy, else None."""
    if initialized(repo_path, comp):
        return _sub(repo_path, comp)
    m = _mirror(repo_path, comp)
    return m if m.is_dir() else None


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
    sub = store_dir(repo_path, comp) or _sub(repo_path, comp)
    head = _out(sub, "symbolic-ref", "--short", "refs/remotes/origin/HEAD")
    if head and "/" in head:
        return head.split("/", 1)[1]
    for b in ("main", "master"):
        if gitops.ref_exists(str(sub), f"refs/remotes/origin/{b}"):
            return b
    return "main"


def fetch(repo_path: str, comp: dict) -> tuple[bool, str]:
    """Network step: bring the component's upstream history up to date.

    A checked-out submodule fetches into its own git dir. One that is not checked out is
    never checked out for this (a hub like TheRock pins llvm-project: tens of GB): its
    upstream's commits and tags go into a tree-less history copy, enough to count commits,
    find releases and bump the pin.
    """
    if not initialized(repo_path, comp):
        m = _mirror(repo_path, comp)
        if not m.is_dir():
            m.parent.mkdir(parents=True, exist_ok=True)
            res = gitops._run(["git", "init", "-q", "--bare", str(m)])
            if res.returncode != 0:
                return False, (res.stderr or res.stdout).strip()
            _git(m, "remote", "add", "origin", comp["url"])
            _git(m, "config", "remote.origin.promisor", "true")
            _git(m, "config", "remote.origin.partialclonefilter", "tree:0")
        elif (_out(m, "remote", "get-url", "origin") or "") != comp["url"]:
            _git(m, "remote", "set-url", "origin", comp["url"])
        res = gitops._run(["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=120", "fetch", "--prune",
                           "--tags", "--filter=tree:0", "origin", "+refs/heads/*:refs/remotes/origin/*"],
                          cwd=str(m), timeout=gitops.FETCH_TIMEOUT)
        if res.returncode != 0:
            return False, (res.stderr or res.stdout).strip()
        _git(m, "remote", "set-head", "origin", "--auto")  # the default branch, also named when a pinned branch is gone
        return True, ""
    sub = _sub(repo_path, comp)
    # .gitmodules says where the upstream is; the submodule's own origin only follows after a sync.
    if (_out(sub, "remote", "get-url", "origin") or "") != comp["url"]:
        _git(repo_path, "submodule", "sync", "--", comp["path"])
    res = gitops._run(["git", "-c", "http.lowSpeedLimit=1000", "-c", "http.lowSpeedTime=120", "fetch", "--prune", "--tags",
                       *(["--filter=blob:none"] if gitops.is_partial(str(sub)) else []), "origin"],
                      cwd=str(sub), timeout=gitops.FETCH_TIMEOUT)
    if res.returncode != 0:
        return False, (res.stderr or res.stdout).strip()
    _git(sub, "remote", "set-head", "origin", "--auto")  # the default branch, also named when a pinned branch is gone
    return True, ""


def compare(repo_path: str, comp: dict, do_fetch: bool = False, light: bool = False) -> dict:
    """State of one component, from what is already fetched unless do_fetch.

    light skips what only the detailed views show (the submodule's own working-tree
    status and the newest missing commits): each git call costs ~0.4s on Windows.
    """
    out = {"name": comp["name"], "path": comp["path"], "url": comp["url"], "enabled": comp.get("enabled", True),
           "watch_paths": comp.get("watch_paths") or [], "after_update": comp.get("after_update") or "",
           "pin": pin(repo_path, comp), "status": "ok", "detail": None, "behind": 0, "local": 0,
           "watched_changes": [], "missing": [], "conflict": False, "conflict_files": []}
    if do_fetch:
        ok, msg = fetch(repo_path, comp)
        if not ok:
            out.update(status="error", detail=f"fetch failed: {msg}")
            return out
    store = store_dir(repo_path, comp)
    if store is None:
        out.update(status="not_checked_out", detail="not compared yet - press Fetch (it downloads only the upstream's history)")
        return out
    sub = str(store)
    out["mode"] = "checkout" if initialized(repo_path, comp) else "history"
    out["branch"] = br = branch(repo_path, comp)
    ref = f"refs/remotes/origin/{br}"
    checked_out = _out(sub, "rev-parse", "HEAD") if out["mode"] == "checkout" else None
    out["checked_out"] = checked_out
    out["moved"] = bool(checked_out and out["pin"] and checked_out != out["pin"])
    out["dirty"] = 0 if light or out["mode"] != "checkout" else \
        len([l for l in (_out(sub, "status", "--porcelain") or "").splitlines() if l.strip()])
    if not out["pin"]:
        out.update(status="error", detail="the hub's HEAD has no pin for this path")
        return out
    if not gitops.ref_exists(sub, ref):
        fetched = _out(sub, "for-each-ref", "--count=1", "--format=%(refname)", "refs/remotes/origin/")
        if fetched:  # the upstream was fetched: the branch itself is missing (renamed upstream?)
            head = _out(sub, "symbolic-ref", "--short", "refs/remotes/origin/HEAD") or ""
            default = head.split("/", 1)[1] if "/" in head else None
            out.update(status="error", detail=f"the upstream has no branch {br} (from .gitmodules or this component's settings)"
                       + (f"; its default branch is {default}: set the component's branch to {default}" if default else ""))
        else:
            out.update(status="not_fetched", detail=f"origin/{br} not fetched yet - press Fetch")
        return out
    if not gitops.ref_exists(sub, out["pin"]):
        out.update(status="not_fetched", detail=("the pinned commit isn't in the upstream's history (your own commits?): "
                                                 "check the submodule out to compare it" if out["mode"] == "history"
                                                 else f"origin/{br} not fetched yet - press Fetch"))
        return out
    out["follow"] = comp.get("follow") or "branch"
    out["pin_tag"] = _out(sub, "describe", "--tags", "--exact-match", out["pin"])
    if out["follow"] == "releases":
        # Compare against the newest release on the branch, not its head: hubs that run
        # release images (one tag per pin) must only move to commits that were released.
        pattern = comp.get("tag_pattern") or "*"
        tags = [t for t in (_out(sub, "tag", "--merged", ref, "--sort=-v:refname", "--list", pattern) or "").split()
                if comp.get("tag_pattern") or _RELEASE.match(t)]
        if not tags:
            out["detail"] = (f"no release tags {'matching ' + pattern if comp.get('tag_pattern') else '(like v1.2.3)'} "
                             f"on origin/{br}: comparing with the branch")
        else:
            wait = float(comp.get("min_age_days") or 0)
            pick = gitops.aged_tag(sub, tags, wait) if wait else {"tag": tags[0], "waiting": [], "ready_at": None}
            if pick["waiting"]:
                out.update(min_age_days=wait, waiting=len(pick["waiting"]), waiting_tags=pick["waiting"][:5], ready_at=pick["ready_at"])
            release = pick["tag"] or None
            if release:
                out["release"] = release
                out["unreleased"] = gitops.behind_ahead(sub, f"{release}^{{commit}}", ref)[0]
                ref = f"refs/tags/{release}^{{commit}}"
            else:
                ref = out["pin"]   # every release is still too new: stay where we are
    elif float(comp.get("min_age_days") or 0) > 0:
        held = gitops.aged_commit(sub, ref, float(comp["min_age_days"]))
        if held["waiting"]:
            out.update(min_age_days=float(comp["min_age_days"]), waiting=held["waiting"], ready_at=held["ready_at"])
            ref = held["sha"] or out["pin"]
    out["upstream"] = gitops.short_sha(sub, ref)
    out["behind"], out["local"] = gitops.behind_ahead(sub, out["pin"], ref)
    if out["follow"] == "branch" and out["behind"] and out["pin_tag"]:
        out["suggest_releases"] = (f"The pin is the release {out['pin_tag']} and the {out['behind']} newer commits are not released yet. "
                                   "If this hub runs release images, set it to follow releases.")
    if out["behind"]:
        # Real changes, not "Merge pull request ..." lines, which say little.
        if not light:
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


def compare_all(repo: dict, do_fetch: bool = False, light: bool = False) -> list[dict]:
    """Every component, compared in parallel (each one is its own git repo, all read-only)."""
    comps = repo.get("components") or []
    # Nothing is checked out here: components that aren't get a history copy (see fetch).
    with ThreadPoolExecutor(max_workers=max(1, min(6, len(comps)))) as pool:
        return list(pool.map(lambda c: _compare_one(repo, c, do_fetch, light), comps))


def _compare_one(repo: dict, c: dict, do_fetch: bool, light: bool) -> dict:
    return (compare(repo["path"], c, do_fetch, light) if c.get("enabled", True) else
            {"name": c["name"], "path": c["path"], "url": c["url"], "enabled": False, "status": "disabled",
             "behind": 0, "local": 0, "watched_changes": [], "missing": []})


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
        sub = str(store_dir(path, comp) or _sub(path, comp))
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
            target = st["upstream"]  # the branch head, or the newest release when following releases
            if st.get("mode") == "history":  # not checked out: just record the new pin
                res = _git(path, "update-index", "--cacheinfo", f"160000,{target},{comp['path']}")
            else:
                res = _git(sub, "checkout", "-q", "--detach", target)
            if res.returncode != 0:
                results.append({"name": name, "ok": False, "detail": (res.stderr or res.stdout).strip()})
                continue
            moved.append((comp, st["pin"], target, st["behind"]))
            results.append({"name": name, "ok": True, "from": st["pin"][:10], "to": target[:10], "behind": st["behind"],
                            "detail": f"{st.get('pin_tag') or st['pin'][:8]} -> {st.get('release') or target[:8]} ({st['behind']} commits)",
                            "release": st.get("release"),
                            "watched_changes": st["watched_changes"],
                            "after_update": comp.get("after_update") if st["watched_changes"] else ""})
    if not moved:
        return {"ok": all(r["ok"] for r in results), "detail": "nothing to bump", "results": results, "commit": None}

    paths = [c["path"] for c, *_ in moved]
    subject = "Bump " + ", ".join(f"{c['name']} to {t[:8]}" for c, _, t, _ in moved)
    body = "\n".join(f"- {c['name']} ({c['path']}): {p[:10]} -> {t[:10]}, {n} upstream commit(s)" for c, p, t, n in moved)
    checked = [c["path"] for c, *_ in moved if initialized(path, c)]
    res = _git(path, "add", "--", *checked) if checked else subprocess.CompletedProcess([], 0, "", "")
    if res.returncode == 0:  # blocker() made sure nothing else is staged
        res = _git(path, "commit", "-q", "-m", subject, "-m", body)
    if res.returncode != 0:
        for comp, old_pin, *_ in moved:  # leave everything as it was
            if initialized(path, comp):
                _git(_sub(path, comp), "checkout", "-q", "--detach", old_pin)
            else:
                _git(path, "update-index", "--cacheinfo", f"160000,{old_pin},{comp['path']}")
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
