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

import os
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

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
