"""GitHub and Docker Hub: push with a token, open PRs, set Actions secrets.

The GitHub token (credentials.github_token) is used two ways:
  * git push - passed to that single git process as an HTTP header through
    git's GIT_CONFIG_* environment, scoped to https://github.com/ only. It is
    never written to .git/config, never on the command line, and other hosts
    never receive it. Without a token, git falls back to whatever login the
    machine already has (e.g. Git Credential Manager).
  * the REST API - opening pull requests and setting repository Actions
    secrets (encrypted with the repo's public key, as GitHub requires).

Recommended token: fine-grained, limited to the repos you manage, with
Contents, Pull requests, Secrets and Actions: read/write, and
Workflows: read/write (needed to push changes under .github/workflows).
"""
from __future__ import annotations

import base64
import re

import requests

from . import credentials, gitops

API = "https://api.github.com"
_GH_URL = re.compile(r"(?:https://(?:[^@/]+@)?github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/([^/]+?)(?:\.git)?/?$")


class GitHubError(Exception):
    pass


def parse_remote(url: str) -> tuple[str, str] | None:
    """(owner, repo) for a github.com remote URL, else None."""
    m = _GH_URL.match((url or "").strip())
    return (m.group(1), m.group(2)) if m else None


def _headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def _api(method: str, path: str, token: str | None = None, **kw):
    token = token or credentials.get("github_token")
    if not token:
        raise GitHubError("no GitHub token saved (Settings -> Accounts)")
    r = requests.request(method, API + path, headers=_headers(token), timeout=30, **kw)
    if r.status_code >= 400:
        try:
            msg = r.json().get("message", "")
            errs = r.json().get("errors")
        except ValueError:
            msg, errs = r.text[:200], None
        raise GitHubError(f"GitHub {r.status_code}: {msg}" + (f" {errs}" if errs else ""))
    return r.json() if r.content else {}


def whoami(token: str | None = None) -> dict:
    """Check a token: the account it belongs to and (classic tokens) its scopes."""
    token = token or credentials.get("github_token")
    if not token:
        raise GitHubError("no GitHub token saved")
    r = requests.get(API + "/user", headers=_headers(token), timeout=20)
    if r.status_code != 200:
        raise GitHubError(f"GitHub rejected the token ({r.status_code})")
    return {"login": r.json().get("login"), "scopes": r.headers.get("X-OAuth-Scopes"),
            "kind": "fine-grained" if (token.startswith("github_pat_")) else "classic or app"}


def auth_env(token: str) -> dict[str, str]:
    """Git config, via environment, that sends the token to github.com only."""
    basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
    return {"GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}"}


def push(repo_path: str, remote: str, branch: str) -> dict:
    """Push a local branch to the same name on `remote`. Never forces."""
    if not re.fullmatch(r"[A-Za-z0-9._/-]+", branch or "") or branch.startswith("-"):
        raise GitHubError("bad branch name")
    url = gitops.remotes(repo_path).get(remote)
    if not url:
        raise GitHubError(f"no remote named {remote}")
    token = credentials.get("github_token")
    extra = auth_env(token) if token and parse_remote(url) and url.startswith("https://") else None
    res = gitops._run(["git", "push", "--porcelain", remote, f"refs/heads/{branch}:refs/heads/{branch}"],
                      cwd=repo_path, extra_env=extra)
    out = (res.stdout + res.stderr).strip()
    if token:
        out = out.replace(token, "****")
    if res.returncode != 0:
        if "rejected" in out or "non-fast-forward" in out:
            raise GitHubError(f"{remote}/{branch} has commits this branch doesn't have; fetch and merge first")
        if "workflow" in out and "scope" in out:
            raise GitHubError("the token can't change .github/workflows: give it the Workflows permission")
        raise GitHubError(out[-600:] or "push failed")
    return {"ok": True, "detail": f"pushed {branch} to {remote}", "auth": "token" if extra else "machine login", "output": out[-600:]}


def open_pr(owner: str, repo: str, head: str, base: str, title: str, body: str) -> dict:
    """Open a PR head -> base, or return the one already open."""
    try:
        pr = _api("POST", f"/repos/{owner}/{repo}/pulls", json={"title": title, "head": head, "base": base, "body": body})
        return {"url": pr["html_url"], "number": pr["number"], "existing": False}
    except GitHubError as e:
        if "already exists" not in str(e):
            raise
    found = _api("GET", f"/repos/{owner}/{repo}/pulls", params={"head": f"{owner}:{head}", "base": base, "state": "open"})
    if not found:
        raise GitHubError("a pull request already exists but could not be found")
    return {"url": found[0]["html_url"], "number": found[0]["number"], "existing": True}


def set_secret(owner: str, repo: str, name: str, value: str) -> None:
    """Create or update a repository Actions secret."""
    from nacl import encoding, public
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", name) or name.startswith("GITHUB_"):
        raise GitHubError(f"invalid secret name {name}")
    key = _api("GET", f"/repos/{owner}/{repo}/actions/secrets/public-key")
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    sealed = base64.b64encode(box.encrypt(value.encode())).decode()
    _api("PUT", f"/repos/{owner}/{repo}/actions/secrets/{name}", json={"encrypted_value": sealed, "key_id": key["key_id"]})


def secret_names(owner: str, repo: str) -> list[str]:
    return [s["name"] for s in _api("GET", f"/repos/{owner}/{repo}/actions/secrets").get("secrets", [])]


def dockerhub_check(username: str | None = None, token: str | None = None) -> dict:
    """Check Docker Hub credentials by logging in to the Hub API."""
    username = username or credentials.get("dockerhub_username")
    token = token or credentials.get("dockerhub_token")
    if not username or not token:
        raise GitHubError("Docker Hub username and access token are both needed")
    r = requests.post("https://hub.docker.com/v2/users/login", json={"username": username, "password": token}, timeout=20)
    if r.status_code != 200:
        raise GitHubError(f"Docker Hub rejected the login ({r.status_code})")
    return {"login": username}
