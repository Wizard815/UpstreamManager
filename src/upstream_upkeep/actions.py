"""GitHub Actions workflows of a repo, with suggestions for forks.

A fork runs every workflow it inherits. Upstream's release, deploy and
scheduled jobs need upstream's secrets and infrastructure, so in a fork they
fail and email you on every run. The fix is to *disable* them on GitHub (a
setting, so the files stay identical to upstream and future pulls never
conflict), not to delete the files.

For each workflow this module combines:
  * GitHub: its id and state (active / disabled), and its recent runs
  * the local clone: its triggers and the secrets it reads (`secrets.X`)
and suggests disabling it in a fork when it runs on a schedule, looks like a
release/deploy job, needs secrets the fork doesn't have, or keeps failing.

It also flags workflows that will arrive with the next upstream pull (they
start active) and ones added since you last reviewed this list.
"""
from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
import time

import requests
import yaml

from . import credentials, github, gitops

WF_DIR = ".github/workflows"
_RELEASE_WORDS = re.compile(
    r"releas|deploy|publish|promot|roll ?out|roll ?back|recover|nightly|beta|preflight|admission|helm|gke|cloud ?run"
    r"|tofu|terraform|firmware|pypi|candidate|emergency|credential|ledger|reconcile|figma|mintlify|cutover", re.I)
_SECRET = re.compile(r"secrets\.([A-Za-z_][A-Za-z0-9_]*)")
_cache: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 60


def _get(path: str, params: dict | None = None):
    """GitHub GET, with the saved token when there is one (public repos work without)."""
    key = path + repr(sorted((params or {}).items()))
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    token = credentials.get("github_token")
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    r = requests.get(github.API + path, headers=headers, params=params, timeout=30)
    if r.status_code == 403 and r.headers.get("X-RateLimit-Remaining") == "0":
        raise github.GitHubError("GitHub's hourly limit for requests without a token is used up; save a token in Settings -> Accounts")
    if r.status_code >= 400:
        raise github.GitHubError(f"GitHub {r.status_code}: {r.json().get('message', '') if r.content else ''}")
    data = r.json()
    _cache[key] = (time.time(), data)
    return data


def clear_cache() -> None:
    _cache.clear()


def local_files(repo_path: str, ref: str = "HEAD") -> list[str]:
    out = gitops._run(["git", "ls-tree", "--name-only", f"{ref}:{WF_DIR}"], cwd=repo_path)
    if out.returncode != 0:
        return []
    return sorted(f"{WF_DIR}/{n}" for n in out.stdout.split() if n.endswith((".yml", ".yaml")))


def file_info(repo_path: str, path: str, ref: str = "HEAD") -> dict:
    """Triggers and secrets of one workflow file, read from git (works in sparse clones)."""
    res = gitops._run(["git", "show", f"{ref}:{path}"], cwd=repo_path)
    if res.returncode != 0:
        return {"triggers": [], "secrets": [], "schedule": False}
    text = res.stdout
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError:
        doc = {}
    on = doc.get("on", doc.get(True, {})) if isinstance(doc, dict) else {}
    triggers = list(on) if isinstance(on, dict) else ([on] if isinstance(on, str) else list(on or []))
    secrets = sorted({s for s in _SECRET.findall(text) if s != "GITHUB_TOKEN"})
    return {"triggers": [str(t) for t in triggers], "secrets": secrets, "schedule": "schedule" in triggers,
            "name": doc.get("name") if isinstance(doc, dict) else None}


def run_stats(owner: str, repo: str) -> dict[int, dict]:
    """Outcome counts of the last 100 runs, per workflow id."""
    runs = _get(f"/repos/{owner}/{repo}/actions/runs", {"per_page": 100}).get("workflow_runs", [])
    stats: dict[int, dict] = {}
    for r in runs:  # newest first
        s = stats.setdefault(r["workflow_id"], {"runs": 0, "failed": 0, "succeeded": 0, "last": None, "last_at": None})
        s["runs"] += 1
        if r.get("conclusion") in ("failure", "timed_out", "startup_failure"):
            s["failed"] += 1
        elif r.get("conclusion") == "success":
            s["succeeded"] += 1
        if s["last"] is None:
            s["last"], s["last_at"] = r.get("conclusion") or r.get("status"), r.get("created_at")
    return stats


def suggest(w: dict, fork: bool, fork_secrets: set[str] | None) -> list[str]:
    """Reasons to disable this workflow in a fork (empty = keep it)."""
    if w["state"] != "active":
        return []
    reasons = []
    st = w.get("stats") or {}
    if st.get("runs", 0) >= 2 and st["failed"] / st["runs"] >= 0.5:
        reasons.append(f"failed {st['failed']} of its last {st['runs']} runs")
    if not fork:
        return reasons
    if w.get("schedule"):
        reasons.append("runs on a schedule")
    if _RELEASE_WORDS.search(f"{w['name']} {w['path']}"):
        reasons.append("looks like an upstream release/deploy job")
    needed = set(w.get("secrets") or [])
    if fork_secrets is not None:
        missing = sorted(needed - fork_secrets)
        if missing:
            reasons.append(f"needs secrets your fork doesn't have: {', '.join(missing[:4])}" + (" …" if len(missing) > 4 else ""))
    elif needed:
        reasons.append(f"needs secrets ({', '.join(sorted(needed)[:4])}" + (" …" if len(needed) > 4 else "") + ")")
    return reasons


def overview(repo: dict, remote: str = "origin") -> dict:
    """Everything the Actions tab shows for one GitHub remote of a repo."""
    url = gitops.remotes(repo["path"]).get(remote)
    gh = github.parse_remote(url or "")
    if not gh:
        return {"ok": False, "error": f"remote {remote} is not a github.com repository"}
    owner, name = gh
    meta = _get(f"/repos/{owner}/{name}")
    fork = bool(meta.get("fork"))
    parent = (meta.get("parent") or {}).get("full_name")
    fork_secrets = None
    if credentials.get("github_token"):
        try:
            fork_secrets = set(github.secret_names(owner, name))
        except github.GitHubError:
            fork_secrets = None  # token can't read secrets: fall back to naming them
    remote_wfs = _get(f"/repos/{owner}/{name}/actions/workflows", {"per_page": 100}).get("workflows", [])
    stats = run_stats(owner, name)
    local = set(local_files(repo["path"]))
    known = set(repo.get("known_workflows") or [])
    # One `git show` per file (~0.2s each on Windows): read them in parallel.
    paths = [w["path"] for w in remote_wfs if w["path"] in local]
    with ThreadPoolExecutor(max_workers=8) as pool:
        infos = dict(zip(paths, pool.map(lambda p: file_info(repo["path"], p), paths)))
    items = []
    for w in remote_wfs:
        if not w["path"].startswith(WF_DIR + "/"):
            continue  # dynamic workflows (Dependabot, Pages, CodeQL default setup)
        info = dict(infos.get(w["path"]) or {"triggers": [], "secrets": [], "schedule": False})
        info.pop("name", None)  # GitHub's name for the workflow wins over the file's
        item = {"id": w["id"], "name": w["name"], "path": w["path"], "state": w["state"], **info,
                "stats": stats.get(w["id"], {"runs": 0, "failed": 0, "succeeded": 0, "last": None, "last_at": None}),
                "new_since_review": bool(known) and w["path"] not in known,
                "in_local_clone": w["path"] in local}
        item["reasons"] = suggest(item, fork, fork_secrets)
        items.append(item)
    items.sort(key=lambda i: (-len(i["reasons"]), -i["stats"]["failed"], i["name"].lower()))
    # Workflows the next pull from upstream would add (they arrive active).
    incoming = []
    for up in repo.get("upstreams") or []:
        if up["name"] == remote:
            continue
        ref = f"refs/remotes/{up['name']}/{up.get('branch') or 'main'}"
        if gitops.ref_exists(repo["path"], ref):
            for path in sorted(set(local_files(repo["path"], ref)) - local):
                incoming.append({"upstream": up["name"], "path": path, **file_info(repo["path"], path, ref)})
    return {"ok": True, "owner": owner, "repo": name, "fork": fork, "parent": parent,
            "token": bool(credentials.get("github_token")), "secrets_known": fork_secrets is not None,
            "workflows": items, "incoming": incoming,
            "reviewed": bool(known), "summary": {
                "total": len(items), "active": sum(i["state"] == "active" for i in items),
                "suggested": sum(bool(i["reasons"]) for i in items),
                "failed_runs": sum(i["stats"]["failed"] for i in items)}}


def set_state(owner: str, repo: str, workflow_id: int, enable: bool) -> None:
    """Enable or disable one workflow (needs a token with Actions: read/write)."""
    github._api("PUT", f"/repos/{owner}/{repo}/actions/workflows/{int(workflow_id)}/{'enable' if enable else 'disable'}")
    clear_cache()
