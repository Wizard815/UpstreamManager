"""The homelab's own Git forge (Forgejo or Gitea), beside GitHub.

Every remote belongs to GitHub, to the home forge, or to neither. The forge is
set by two values in Settings -> Accounts (or the environment):

  forge_url    UPSTREAM_FORGE_URL    e.g. http://forgejo:3000 or http://192.168.20.5:3030
  forge_token  UPSTREAM_FORGE_TOKEN  an access token with write:repository, write:issue, read:user

The same forge is often reached under more than one address (its name on
ai_net and the Unraid IP). UPSTREAM_FORGE_HOSTS lists the others, comma
separated (host or host:port), so remotes cloned through any of them are
recognised. The forge token goes only to these hosts, exactly like the GitHub
token goes only to github.com.

Forgejo and Gitea share one API (/api/v1), so everything here works on both.
Mirrors are the forge's own: a push mirror sends every push on the forge to
GitHub, a pull mirror copies a GitHub repo onto the forge on a schedule.
"""
from __future__ import annotations

import base64
import os
import re
import time
from dataclasses import dataclass
from urllib.parse import quote, urlsplit

import requests

from . import credentials
from .github import GitHubError, parse_remote as parse_github


class ForgeError(GitHubError):
    """A forge failure. A GitHubError subclass, so every place that already
    reports push and PR errors reports these the same way."""


@dataclass(frozen=True)
class Remote:
    forge: str   # "github" or "forge"
    owner: str
    repo: str

    def web_url(self) -> str:
        base = "https://github.com" if self.forge == "github" else url()
        return f"{base}/{self.owner}/{self.repo}"


def url() -> str:
    """The forge's base URL, without a trailing slash; "" when none is set."""
    return (credentials.get("forge_url") or "").strip().rstrip("/")


def configured() -> bool:
    return bool(url())


def _netloc(u: str) -> str:
    parts = urlsplit(u if "://" in u else f"http://{u}")
    return (parts.hostname or "").lower() + (f":{parts.port}" if parts.port else "")


def hosts() -> set[str]:
    """host or host:port values that mean "the home forge"."""
    out = {_netloc(url())} if url() else set()
    for h in (os.environ.get("UPSTREAM_FORGE_HOSTS") or "").split(","):
        if h.strip():
            out.add(_netloc(h.strip()))
    out.discard("")
    return out


def _prefix() -> str:
    """Path the forge is served under (Forgejo behind a reverse proxy at /git), else ""."""
    return urlsplit(url()).path.rstrip("/") if url() else ""


_SCP = re.compile(r"^(?:[^@/]+@)?([^:/]+):(?!//)(.+)$")  # git@host:owner/repo.git


def parse(remote_url: str) -> Remote | None:
    """Which forge a remote URL points at, with its owner and repo; None for other hosts."""
    remote_url = (remote_url or "").strip()
    gh = parse_github(remote_url)
    if gh:
        return Remote("github", *gh)
    known = hosts()
    if not known:
        return None
    names = {h.split(":")[0] for h in known}
    m = _SCP.match(remote_url) if "://" not in remote_url else None
    if m:
        host, path = m.group(1).lower(), m.group(2)
        if host not in names:
            return None
    else:
        parts = urlsplit(remote_url)
        if parts.scheme not in ("http", "https", "ssh"):
            return None
        host = (parts.hostname or "").lower()
        netloc = host + (f":{parts.port}" if parts.port else "")
        # HTTP must match host and port; SSH runs on its own port, so the host name is enough.
        if (parts.scheme == "ssh" and host not in names) or (parts.scheme != "ssh" and netloc not in known):
            return None
        path = parts.path
        if parts.scheme != "ssh" and _prefix() and path.startswith(_prefix() + "/"):
            path = path[len(_prefix()):]
    bits = [b for b in path.strip("/").split("/") if b]
    if len(bits) != 2:
        return None
    owner, repo = bits[0], re.sub(r"\.git$", "", bits[1])
    if not owner or not repo:
        return None
    return Remote("forge", owner, repo)


def sends_token_to(remote_url: str) -> bool:
    """True when a push to this URL should carry the forge token (http(s) on a forge host)."""
    r = parse(remote_url)
    return bool(r and r.forge == "forge" and remote_url.startswith(("http://", "https://")))


def auth_env(token: str) -> dict[str, str]:
    """Git config, via environment, that sends the token to the forge's hosts only."""
    env: dict[str, str] = {}
    targets = []
    for h in sorted(hosts()):
        for scheme in ("http", "https"):
            targets.append(f"{scheme}://{h}/")
    for i, t in enumerate(targets):
        env[f"GIT_CONFIG_KEY_{i}"] = f"http.{t}.extraheader"
        env[f"GIT_CONFIG_VALUE_{i}"] = f"Authorization: token {token}"
    env["GIT_CONFIG_COUNT"] = str(len(targets))
    return env


def _token(token: str | None) -> str:
    token = token or credentials.get("forge_token")
    if not url():
        raise ForgeError("no forge set up (Settings -> Accounts: forge address and token)")
    if not token:
        raise ForgeError("no forge token saved (Settings -> Accounts)")
    return token


def _api(method: str, path: str, token: str | None = None, ok404: bool = False, **kw):
    token = _token(token)
    try:
        kw.setdefault("timeout", 30)
        r = requests.request(method, f"{url()}/api/v1{path}",
                             headers={"Authorization": f"token {token}", "Accept": "application/json"}, **kw)
    except requests.RequestException as e:
        raise ForgeError(f"can't reach the forge at {url()}: {e.__class__.__name__}. Is the forgejo container running?")
    if ok404 and r.status_code == 404:
        return None
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", "")
        except ValueError:
            msg = r.text[:200]
        if r.status_code == 401:
            msg = msg or "the forge rejected the token"
        raise ForgeError(f"forge {r.status_code}: {msg}")
    return r.json() if r.content else {}


def whoami(token: str | None = None) -> dict:
    me = _api("GET", "/user", token)
    version = (_api("GET", "/version", token) or {}).get("version")
    return {"login": me.get("login"), "scopes": None, "kind": "forge", "version": version, "url": url()}


# ---------------------------------------------------------------- pull requests and secrets
def open_pr(owner: str, repo: str, head: str, base: str, title: str, body: str) -> dict:
    """Open a PR head -> base on the forge, or return the one already open."""
    # Right after a push the forge may not know the branch yet (it records pushes
    # in the background), so a 404 is retried for a few seconds.
    for attempt in range(6):
        try:
            pr = _api("POST", f"/repos/{owner}/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
            return {"url": pr["html_url"], "number": pr["number"], "existing": False}
        except ForgeError as e:
            if "404" in str(e) and attempt < 5:
                time.sleep(1)
                continue
            if "409" not in str(e) and "already exists" not in str(e):
                raise
            break
    pr = _api("GET", f"/repos/{owner}/{repo}/pulls/{base}/{head}", ok404=True)
    if not pr:
        raise ForgeError("a pull request already exists but could not be found")
    return {"url": pr["html_url"], "number": pr["number"], "existing": True}


def set_secret(owner: str, repo: str, name: str, value: str) -> None:
    """Create or update a repository Actions secret (the forge encrypts it itself)."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or name.startswith(("GITHUB_", "GITEA_", "FORGEJO_")):
        raise ForgeError(f"invalid secret name {name}")
    _api("PUT", f"/repos/{owner}/{repo}/actions/secrets/{name}", json={"data": value})


def secret_names(owner: str, repo: str) -> list[str]:
    return [s["name"] for s in (_api("GET", f"/repos/{owner}/{repo}/actions/secrets") or [])]


# ---------------------------------------------------------------- repos and mirrors
def create_repo(name: str, private: bool = True, description: str = "", org: str | None = None) -> dict:
    """A new, empty repo on the forge (private unless asked otherwise: the API's default is public)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name or ""):
        raise ForgeError("repo names may only use letters, digits, '.', '_' and '-'")
    body = {"name": name, "private": bool(private), "description": description[:255]}
    path = f"/orgs/{org}/repos" if org else "/user/repos"
    try:
        r = _api("POST", path, json=body)
    except ForgeError as e:
        if "409" in str(e):
            raise ForgeError(f"a repo named {name} already exists on the forge")
        raise
    return {"owner": r["owner"]["login"], "repo": r["name"], "clone_url": r["clone_url"],
            "web_url": r["html_url"], "private": r["private"]}


def repo_info(owner: str, repo: str) -> dict | None:
    r = _api("GET", f"/repos/{owner}/{repo}", ok404=True)
    if r is None:
        return None
    return {"owner": owner, "repo": repo, "private": r.get("private"), "mirror": r.get("mirror"),
            "original_url": r.get("original_url") or None, "mirror_interval": r.get("mirror_interval") or None,
            "mirror_updated": r.get("mirror_updated"), "web_url": r.get("html_url"),
            "default_branch": r.get("default_branch")}


def push_mirrors(owner: str, repo: str) -> list[dict]:
    return [{"name": m.get("remote_name"), "address": m.get("remote_address"), "interval": m.get("interval"),
             "sync_on_commit": m.get("sync_on_commit"), "last_update": m.get("last_update"),
             "last_error": m.get("last_error") or None}
            for m in (_api("GET", f"/repos/{owner}/{repo}/push_mirrors") or [])]


def _github_login() -> tuple[str, str]:
    token = credentials.get("github_token")
    if not token:
        raise ForgeError("mirroring with GitHub needs a GitHub token (Settings -> Accounts)")
    return "x-access-token", token


def add_push_mirror(owner: str, repo: str, github_url: str, interval: str = "8h", sync_on_commit: bool = True) -> dict:
    """Mirror the forge repo to GitHub: every push to the forge goes on to github_url.

    The forge keeps the GitHub token for this mirror (it has to, to push later);
    it is the saved GitHub token, so give that token write access only to the
    repos you mirror."""
    gh = parse_github(github_url)
    if not gh:
        raise ForgeError("a push mirror needs a github.com repository URL")
    if any(m["address"] and parse_github(m["address"]) == gh for m in push_mirrors(owner, repo)):
        raise ForgeError(f"{owner}/{repo} already mirrors to github.com/{gh[0]}/{gh[1]}")
    user, token = _github_login()
    _api("POST", f"/repos/{owner}/{repo}/push_mirrors", json={
        "remote_address": f"https://github.com/{gh[0]}/{gh[1]}.git", "remote_username": user,
        "remote_password": token, "interval": interval, "sync_on_commit": bool(sync_on_commit)})
    return {"ok": True, "detail": f"{owner}/{repo} on the forge now mirrors to github.com/{gh[0]}/{gh[1]}"}


def remove_push_mirror(owner: str, repo: str, name: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9._-]+", name or ""):
        raise ForgeError("bad mirror name")
    _api("DELETE", f"/repos/{owner}/{repo}/push_mirrors/{name}")


def sync(owner: str, repo: str) -> dict:
    """Run the repo's mirrors now: a pull mirror fetches, push mirrors push."""
    info = repo_info(owner, repo)
    if info is None:
        raise ForgeError(f"{owner}/{repo} isn't on the forge")
    if info["mirror"]:
        _api("POST", f"/repos/{owner}/{repo}/mirror-sync")
        return {"ok": True, "detail": f"fetching {info['original_url'] or 'the source'} into {owner}/{repo}"}
    if not push_mirrors(owner, repo):
        raise ForgeError(f"{owner}/{repo} has no mirrors")
    _api("POST", f"/repos/{owner}/{repo}/push_mirrors-sync")
    return {"ok": True, "detail": f"pushing {owner}/{repo} to its mirrors"}


def import_mirror(source_url: str, name: str | None = None, private: bool = True,
                  interval: str = "8h", owner: str | None = None) -> dict:
    """Copy a GitHub (or any https) repo onto the forge as a pull mirror that keeps fetching.

    A private GitHub repo needs the saved GitHub token, which the forge then
    keeps to fetch it on schedule."""
    if not source_url.startswith("https://"):
        raise ForgeError("the source must be an https:// clone URL (the forge refuses plain http)")
    gh = parse_github(source_url)
    name = name or (gh[1] if gh else re.sub(r"\.git$", "", source_url.rstrip("/").rsplit("/", 1)[-1]))
    body = {"clone_addr": source_url, "repo_name": name, "mirror": True, "private": bool(private),
            "mirror_interval": interval, "service": "git"}
    if owner:
        body["repo_owner"] = owner
    if gh and credentials.get("github_token"):
        body["auth_username"], body["auth_password"] = _github_login()
    try:
        r = _api("POST", "/repos/migrate", json=body, timeout=600)
    except ForgeError as e:
        if "409" in str(e):
            raise ForgeError(f"a repo named {name} already exists on the forge")
        raise
    return {"owner": r["owner"]["login"], "repo": r["name"], "clone_url": r["clone_url"], "web_url": r["html_url"],
            "detail": f"{source_url} is mirrored to {r['html_url']} every {interval}"}


# ------------------------------------------------------------ repos as a base for new ones
def _made(r: dict) -> dict:
    return {"owner": r["owner"]["login"], "repo": r["name"], "clone_url": r["clone_url"],
            "web_url": r["html_url"], "private": r.get("private")}


def search_repos(query: str = "", limit: int = 30) -> list[dict]:
    """Repos on the forge the token can see (to pick a base), best match first."""
    data = _api("GET", "/repos/search", params={"q": (query or "")[:100], "limit": max(1, min(int(limit), 50))}) or {}
    return [{"full_name": r.get("full_name"), "description": r.get("description") or "", "private": r.get("private"),
             "template": r.get("template"), "mirror": r.get("mirror"), "fork": r.get("fork"),
             "clone_url": r.get("clone_url"), "web_url": r.get("html_url"), "default_branch": r.get("default_branch")}
            for r in (data.get("data") if isinstance(data, dict) else data) or []]


def fork(owner: str, repo: str, name: str = "", org: str | None = None, private: bool = True) -> dict:
    """The forge's own fork: it keeps the link to the base (shown on the forge, PRs back to it work).
    A fork starts with the base's visibility; private makes it private afterwards."""
    body = {"name": name} if name else {}
    if org:
        body["organization"] = org
    try:
        made = _made(_api("POST", f"/repos/{quote(owner)}/{quote(repo)}/forks", json=body, timeout=300))
    except ForgeError as e:
        if "409" in str(e):
            raise ForgeError(f"{name or repo} already exists there, or that account already has a fork of {owner}/{repo} (one per account): give another name, or fork into an org")
        raise
    except KeyError:
        raise ForgeError("the forge answered the fork without the new repo")
    if private and not made["private"]:
        try:
            made["private"] = bool(_api("PATCH", f"/repos/{quote(made['owner'])}/{quote(made['repo'])}", json={"private": True}).get("private"))
        except ForgeError as e:
            made["visibility_error"] = str(e)
        if not made["private"]:
            made.setdefault("visibility_error", "the forge keeps a fork as visible as its base: use copy for a private repo")
    return made


def generate(owner: str, repo: str, name: str, private: bool = True, new_owner: str = "") -> dict:
    """A new repo from a template repo (its files, fresh history; no link back)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]{1,100}", name or ""):
        raise ForgeError("repo names may only use letters, digits, '.', '_' and '-'")
    me = new_owner or _api("GET", "/user")["login"]
    try:
        return _made(_api("POST", f"/repos/{quote(owner)}/{quote(repo)}/generate", timeout=300, json={
            "owner": me, "name": name, "private": bool(private), "git_content": True, "topics": True, "labels": True,
            "webhooks": False, "avatar": True, "git_hooks": False}))
    except ForgeError as e:
        if "422" in str(e) or "404" in str(e):
            raise ForgeError(f"{owner}/{repo} isn't a template repo: tick \"Template\" in its settings on the forge, or pick copy/fork")
        if "409" in str(e):
            raise ForgeError(f"a repo named {name} already exists on the forge")
        raise


# ------------------------------------------------------------ webhooks: the forge tells Upstream about pushes
def hooks(owner: str, repo: str) -> list[dict]:
    return [{"id": h.get("id"), "url": (h.get("config") or {}).get("url"), "events": h.get("events"), "active": h.get("active")}
            for h in (_api("GET", f"/repos/{quote(owner)}/{quote(repo)}/hooks") or [])]


def add_push_hook(owner: str, repo: str, target: str, secret: str) -> dict:
    """A push webhook to Upstream (type gitea: Forgejo and Gitea both send it, signed with the secret)."""
    if not target.startswith(("http://", "https://")):
        raise ForgeError("Upstream's address as the forge reaches it starts with http:// or https://")
    for h in hooks(owner, repo):
        if h["url"] == target:
            return {"ok": True, "detail": f"{owner}/{repo} already tells Upstream about pushes", "id": h["id"], "new": False}
    h = _api("POST", f"/repos/{quote(owner)}/{quote(repo)}/hooks", json={
        "type": "gitea", "active": True, "events": ["push", "create", "delete"], "branch_filter": "*",
        "config": {"url": target, "content_type": "json", "secret": secret}})
    return {"ok": True, "detail": f"{owner}/{repo} now tells Upstream about every push", "id": h.get("id"), "new": True}


def verify_hook(body: bytes, headers, secret: str) -> bool:
    """A delivery signed with the secret: X-Forgejo-Signature / X-Gitea-Signature (hex) or X-Hub-Signature-256."""
    import hashlib
    import hmac
    if not secret:
        return False
    want = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    for name in ("X-Forgejo-Signature", "X-Gitea-Signature"):
        got = (headers.get(name) or "").strip()
        if got and hmac.compare_digest(got, want):
            return True
    got = (headers.get("X-Hub-Signature-256") or "").strip()
    return bool(got) and hmac.compare_digest(got, "sha256=" + want)


# ------------------------------------------------------------ CI and issues
_RUN_STATE = {"success": "success", "failure": "failure", "cancelled": "cancelled", "skipped": "skipped",
              "running": None, "waiting": None, "blocked": None}


def actions_health(owner: str, repo: str) -> dict:
    """Forgejo Actions in the same shape as actions.health(): each workflow's latest run and its last 10.

    Forgejo has no workflow list in its API, so workflows are the ones that ran (newest 50 runs)."""
    data = _api("GET", f"/repos/{quote(owner)}/{quote(repo)}/actions/tasks", params={"limit": 50}, ok404=True)
    runs = (data or {}).get("workflow_runs") or []
    by_wf: dict[str, list] = {}
    for r in runs:  # newest first
        by_wf.setdefault(r.get("workflow_id") or r.get("name") or "?", []).append(r)
    items = []
    for wf, rs in by_wf.items():
        last = rs[0]
        status = last.get("status") or ""
        items.append({
            "name": rs[0].get("name") or wf, "path": f".forgejo/workflows/{wf}" if "/" not in wf else wf, "state": "active",
            "html_url": f"{url()}/{owner}/{repo}/actions?workflow={wf}",
            "last": {"conclusion": _RUN_STATE.get(status, status), "status": "completed" if _RUN_STATE.get(status) else status,
                     "url": last.get("url"), "branch": last.get("head_branch"), "event": last.get("event"),
                     "at": last.get("created_at"), "title": last.get("display_title")},
            "recent": [_RUN_STATE.get(r.get("status"), r.get("status")) or r.get("status") for r in rs[:10]],
        })
    return {"owner": owner, "repo": repo, "url": f"{url()}/{owner}/{repo}/actions", "items": items,
            "actions_off": data is None}


def issues(owner: str, repo: str, state: str = "open", limit: int = 50) -> list[dict]:
    """Issues (not PRs), newest change first, in GitHub's shape (html_url, user.login, labels)."""
    return _api("GET", f"/repos/{quote(owner)}/{quote(repo)}/issues",
                params={"state": state, "type": "issues", "limit": max(1, min(int(limit), 50)), "sort": "recentupdate"}) or []


def issue(owner: str, repo: str, number: int) -> dict:
    return _api("GET", f"/repos/{quote(owner)}/{quote(repo)}/issues/{int(number)}")


# ------------------------------------------------------------ jobs on the forge's runners
# One command on one of the forge's own runners (e.g. ubuntu-latest, or a GPU runner labelled gpu),
# optionally in a given container image and with a forge repo checked out. Jobs go through a private
# jobs repo (LOGIN/hermes-jobs unless Settings -> Accounts names another) that holds one
# workflow_dispatch workflow. Hermes's forgejo skill (`forge.py job`) writes the very same file to the
# same repo, so the two share it; keep hermes-job.yml identical to the skill's templates/hermes-job.yml.
JOB_WORKFLOW = ".forgejo/workflows/hermes-job.yml"
_TEMPLATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hermes-job.yml")
DONE = {"success", "failure", "cancelled", "skipped"}
_MARK = "=== hermes job output ==="
_STAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ")
_REPO = re.compile(r"^\w[\w.-]*/\w[\w.-]*$")


def jobs_repo() -> tuple[str, str]:
    """owner, name of the jobs repo: Settings -> Accounts (forge_jobs_repo), else LOGIN/hermes-jobs."""
    named = (credentials.get("forge_jobs_repo") or "").strip()
    if named:
        if not _REPO.match(named):
            raise ForgeError(f"the jobs repo is OWNER/REPO on the forge, not {named!r} (Settings -> Accounts)")
        owner, name = named.split("/", 1)
        return owner, name
    return _api("GET", "/user")["login"], "hermes-jobs"


def job_setup() -> dict:
    """Make the jobs repo (private) if needed, write the job workflow, and set the JOBS_TOKEN secret jobs
    check out repos with (UPSTREAM_FORGE_JOBS_TOKEN, a read-only token, else the forge token)."""
    owner, name = jobs_repo()
    if _api("GET", f"/repos/{quote(owner)}/{quote(name)}", ok404=True) is None:
        me = _api("GET", "/user")["login"]
        path = "/user/repos" if owner == me else f"/orgs/{quote(owner)}/repos"
        _api("POST", path, json={"name": name, "private": True, "auto_init": True,
                                 "description": "Jobs run on the forge's runners (Hermes forgejo skill, Upstream Manager)"})
    with open(_TEMPLATE, encoding="utf-8") as f:
        wanted = f.read()
    content = base64.b64encode(wanted.encode()).decode()
    cur = _api("GET", f"/repos/{quote(owner)}/{quote(name)}/contents/{JOB_WORKFLOW}", ok404=True)
    if cur is None:
        _api("POST", f"/repos/{quote(owner)}/{quote(name)}/contents/{JOB_WORKFLOW}", json={"content": content, "message": "job workflow"})
        workflow = "created"
    elif base64.b64decode(cur.get("content") or "").decode("utf-8", "replace") != wanted:
        _api("PUT", f"/repos/{quote(owner)}/{quote(name)}/contents/{JOB_WORKFLOW}",
             json={"content": content, "sha": cur["sha"], "message": "update job workflow"})
        workflow = "updated"
    else:
        workflow = "unchanged"
    read_only = os.environ.get("UPSTREAM_FORGE_JOBS_TOKEN", "").strip()
    _api("PUT", f"/repos/{quote(owner)}/{quote(name)}/actions/secrets/JOBS_TOKEN", json={"data": read_only or _token(None)})
    return {"ok": True, "jobs_repo": f"{owner}/{name}", "workflow": workflow,
            "checkout_token": "UPSTREAM_FORGE_JOBS_TOKEN" if read_only else "the forge token"}


def _api_text(path: str) -> str:
    token = _token(None)
    try:
        r = requests.get(f"{url()}/api/v1{path}", headers={"Authorization": f"token {token}"}, timeout=30)
    except requests.RequestException as e:
        raise ForgeError(f"can't reach the forge at {url()}: {e.__class__.__name__}")
    if r.status_code >= 400:
        raise ForgeError(f"forge {r.status_code} reading the job log")
    return r.text


def job_output(log: str, limit: int = 80) -> str:
    """The command's own lines (after the workflow's marker), without timestamps; the log's tail if no marker."""
    lines = [_STAMP.sub("", line) for line in log.rstrip().splitlines()]
    if _MARK in lines:
        lines = lines[lines.index(_MARK) + 1:]
        end = next((i for i, line in enumerate(lines) if line.startswith(("⭐ Run Post", "🏁"))), len(lines))
        lines = lines[:end]
    return "\n".join(lines[-max(1, int(limit)):])


def job_status(run_id: int, logs: int = 80) -> dict:
    owner, name = jobs_repo()
    base = f"/repos/{quote(owner)}/{quote(name)}/actions/runs/{int(run_id)}"
    run = _api("GET", base)
    jobs = _api("GET", f"{base}/jobs") or []
    jobs = jobs if isinstance(jobs, list) else jobs.get("jobs") or []
    ran = [j for j in jobs if j.get("status") != "skipped"] or jobs
    out = {"ok": True, "run": int(run_id), "status": run.get("status"), "done": run.get("status") in DONE,
           "title": run.get("title") or run.get("display_title"), "url": run.get("html_url"),
           "jobs": [{"name": j.get("name"), "status": j.get("status"), "runs_on": j.get("runs_on")} for j in ran]}
    if out["status"] == "waiting":
        out["note"] = "waiting: no idle runner has this label yet"
    if logs and ran and ran[0].get("status") in DONE:
        text = job_output(_api_text(f"/repos/{quote(owner)}/{quote(name)}/actions/jobs/{int(ran[0]['id'])}/logs"), logs)
        tok = credentials.get("forge_token") or ""
        out["output"] = text.replace(tok, "****") if tok else text
    return out


def job_run(command: str, runner: str = "ubuntu-latest", image: str = "", repo: str = "", ref: str = "", title: str = "") -> dict:
    """Dispatch one job and return at once with its run id (job_status follows it)."""
    command = (command or "").strip()
    if not command:
        raise ForgeError("give the command to run")
    runner = (runner or "ubuntu-latest").strip()
    if not re.match(r"^[\w.-]{1,64}$", runner):
        raise ForgeError("runner is a runner label, e.g. ubuntu-latest or gpu")
    if repo and not _REPO.match(repo):
        raise ForgeError("repo is OWNER/REPO on the forge")
    if image and not re.match(r"^[\w][\w.\-/:@]{0,299}$", image):
        raise ForgeError("image is a container image name, e.g. node:24-bookworm")
    if ref and not re.match(r"^[\w./-]{1,200}$", ref):
        raise ForgeError("ref is a branch, tag or commit")
    owner, name = jobs_repo()
    inputs = {"runner": runner, "image": image or "", "repo": repo or "", "ref": ref or "",
              "command": command, "title": (title or command)[:120]}
    try:
        run = _api("POST", f"/repos/{quote(owner)}/{quote(name)}/actions/workflows/hermes-job.yml/dispatches",
                   json={"ref": "main", "inputs": inputs, "return_run_info": True})
    except ForgeError as e:
        if "404" in str(e):
            raise ForgeError(f"no job workflow in {owner}/{name} yet: press Set up jobs (or POST /api/forge/jobs/setup) once") from None
        raise
    return job_status(int(run["id"]), logs=0)


def runners() -> list[dict]:
    """The forge's runners and their labels (needs an admin token, else the user's own runners)."""
    for path in ("/admin/actions/runners", "/user/actions/runners"):
        try:
            data = _api("GET", path)
        except ForgeError:
            continue
        items = data if isinstance(data, list) else (data or {}).get("runners") or (data or {}).get("entries") or []
        return [{"name": r.get("name"), "status": r.get("status"),
                 "labels": [lb.get("name") if isinstance(lb, dict) else lb for lb in r.get("labels") or []]} for r in items]
    raise ForgeError("the forge token can't list runners (that needs an admin token); type the label, e.g. ubuntu-latest or gpu")
