"""Git operations: inspect, fetch, compare, browse.

All git work is done through a child `git` process so we never assume git
is installed in-process and so credentials stay out of our memory.
"""
from __future__ import annotations

import subprocess
import sys
import re
from pathlib import Path


# Windows needs these for DNS/TLS and credential helpers; git run without them
# cannot even resolve github.com. Everything else (tokens, API keys) is withheld.
_ENV_KEYS = {
    "PATH", "HOME", "LANG", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "USERPROFILE",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "COMSPEC", "PATHEXT", "HOMEDRIVE",
    "HOMEPATH", "USERNAME", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "SSL_CERT_FILE",
}


def _run(args: list[str], cwd: str | None = None, check: bool = False) -> subprocess.CompletedProcess:
    env = {k: v for k, v in __import__("os").environ.items() if k.upper() in _ENV_KEYS}
    env["GIT_TERMINAL_PROMPT"] = "0"  # never hang waiting for a password
    return subprocess.run(
        args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
        env=env, check=check,
    )


class GitError(Exception):
    pass


def _git(args: list[str], cwd: str) -> str:
    res = _run(["git", *args], cwd=cwd)
    if res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip() or 'unknown error'}")
    return res.stdout.strip()


def _remotes(path: str) -> dict[str, str]:
    try:
        out = _git(["remote", "-v"], path)
    except GitError:
        return {}
    remotes: dict[str, str] = {}
    for line in out.splitlines():
        if "\t" not in line:
            continue
        name, url = line.split("\t", 1)
        url = url.strip()
        # git remote -v appends ' (fetch)' or ' (push)' to each URL
        url = re.sub(r"\s*\(push\)$", "", url)
        url = re.sub(r"\s*\(fetch\)$", "", url)
        remotes[name.strip().lower()] = url.strip()
    return remotes


def is_git_repo(path: str) -> bool:
    return _git(["rev-parse", "--is-inside-work-tree"], path) == "true"


def repo_name(path: str) -> str | None:
    try:
        top = _git(["rev-parse", "--show-toplevel"], path)
        return Path(top).name
    except GitError:
        return None


def remotes(path: str) -> dict[str, str]:
    """Public wrapper over _remotes: {name: url} for every remote."""
    return _remotes(path)


def clone_into(dest: str, url: str) -> None:
    """Clone `url` into `dest`."""
    res = _run(["git", "clone", url, dest], check=False)
    if res.returncode != 0:
        raise GitError(f"clone failed: {res.stderr.strip() or res.stdout.strip() or 'unknown error'}")


def current_branch_name(path: str) -> str | None:
    try:
        return _git(["rev-parse", "--abbrev-ref", "HEAD"], path)
    except GitError:
        return None


def current_branch(path: str) -> str | None:
    return current_branch_name(path)


def recent_commits(path: str, n: int = 20, with_files: bool = False) -> list[dict]:
    """Newest-first commits on HEAD: {sha, date, author, subject[, files]}."""
    sep = "\x1f"
    args = ["log", f"-{n}", f"--format=%x1e%h{sep}%aI{sep}%an{sep}%s"]
    if with_files:
        args.append("--name-only")
    try:
        out = _git(args, path)
    except GitError:
        return []
    commits = []
    for block in out.split("\x1e"):
        if not block.strip():
            continue
        head, *rest = block.strip().split("\n")
        sha, date, author, subject = (head.split(sep) + ["", "", "", ""])[:4]
        c = {"sha": sha, "date": date, "author": author, "subject": subject}
        if with_files:
            c["files"] = [f for f in rest if f.strip()]
        commits.append(c)
    return commits


def short_sha(path: str, rev: str | None = None) -> str | None:
    try:
        if rev is None:
            return _git(["rev-parse", "HEAD"], path)
        return _git(["rev-parse", rev], path)
    except GitError:
        return None


# ------------------------------------------------------------ compare / browse
def fetch(path: str, remote: str) -> tuple[bool, str]:
    """Update refs/remotes/<remote>/* from the network. Returns (ok, message)."""
    res = _run(["git", "fetch", "--prune", remote], cwd=path)
    return res.returncode == 0, (res.stderr or res.stdout).strip()


def ref_exists(path: str, ref: str) -> bool:
    return _run(["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=path).returncode == 0


def behind_ahead(path: str, local_ref: str, remote_ref: str) -> tuple[int, int]:
    """(behind, ahead): commits the remote has that local lacks, and the reverse."""
    out = _run(["git", "rev-list", "--left-right", "--count", f"{remote_ref}...{local_ref}"], cwd=path)
    if out.returncode != 0:
        return 0, 0
    behind, ahead = out.stdout.split()
    return int(behind), int(ahead)


def merge_check(path: str, local_ref: str, remote_ref: str) -> dict:
    """Would merging remote_ref into local_ref conflict? Never touches the working tree.

    Uses `git merge-tree --write-tree`: exit 0 = clean, 1 = conflicts (file names listed).
    """
    res = _run(["git", "merge-tree", "--write-tree", "--name-only", "--no-messages", local_ref, remote_ref], cwd=path)
    if res.returncode == 0:
        return {"ok": True, "conflict": False, "files": []}
    if res.returncode == 1:
        lines = res.stdout.splitlines()[1:]  # first line is the (partial) tree id
        return {"ok": True, "conflict": True, "files": [l for l in lines if l.strip()]}
    return {"ok": False, "conflict": False, "files": [], "error": (res.stderr or res.stdout).strip()}


def list_commits(path: str, rev: str, skip: int = 0, limit: int = 50, query: str = "") -> list[dict]:
    """Commits for a rev expression (a ref or 'a..b'), newest first, paged."""
    sep = "\x1f"
    args = ["log", f"--skip={skip}", f"-{limit}", f"--format=%H{sep}%h{sep}%aI{sep}%an{sep}%s", rev]
    if query:
        args = ["log", f"--skip={skip}", f"-{limit}", "-i", f"--grep={query}", f"--format=%H{sep}%h{sep}%aI{sep}%an{sep}%s", rev]
    res = _run(["git", *args], cwd=path)
    if res.returncode != 0:
        return []
    out = []
    for line in res.stdout.splitlines():
        parts = line.split(sep)
        if len(parts) == 5:
            out.append({"full": parts[0], "sha": parts[1], "date": parts[2], "author": parts[3], "subject": parts[4]})
    return out


def commit_detail(path: str, sha: str, max_chars: int = 20000) -> dict:
    """Message, stat and patch of one commit (patch truncated)."""
    if not re.fullmatch(r"[0-9a-fA-F]{4,40}", sha or ""):
        raise GitError("bad commit id")
    head = _run(["git", "show", "--no-patch", "--format=%H%n%an <%ae>%n%aI%n%B", sha], cwd=path)
    if head.returncode != 0:
        raise GitError(head.stderr.strip() or "unknown commit")
    full, author, date, *msg = head.stdout.split("\n")
    stat = _run(["git", "show", "--format=", "--stat=120", sha], cwd=path).stdout.strip()
    patch = _run(["git", "show", "--format=", "--patch", sha], cwd=path).stdout
    return {"sha": full, "author": author, "date": date, "message": "\n".join(msg).strip(), "stat": stat,
            "patch": patch[:max_chars], "truncated": len(patch) > max_chars}


def status_summary(path: str) -> dict:
    """Branch, dirty file count and a few changed paths, for the chat context."""
    res = _run(["git", "status", "--porcelain"], cwd=path)
    lines = [l for l in res.stdout.splitlines() if l.strip()] if res.returncode == 0 else []
    return {"branch": current_branch_name(path), "dirty": len(lines), "changed": [l[3:] for l in lines[:15]]}
