"""Git detection/commit/push operations.

All git work is done through a child `git` process so we never assume git
is installed in-process and so credentials stay out of our memory.
"""
from __future__ import annotations

import subprocess
import sys
import re
from dataclasses import dataclass
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


@dataclass
class RepoState:
    path: str
    is_repo: bool
    name: str | None
    remotes: dict[str, str]
    base_ref: str | None
    local_sha: str | None
    upstream_sha: str | None
    recent_sha: str | None
    upstream_branch: str | None
    base_dirty: bool
    ahead_by: int
    behind_by: int
    last_commit: str | None


class GitError(Exception):
    pass


def _git(args: list[str], cwd: str) -> str:
    res = _run(["git", *args], cwd=cwd)
    if res.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {res.stderr.strip() or res.stdout.strip() or 'unknown error'}")
    return res.stdout.strip()


def resolve_remote_name(path: str, preferred: str | None) -> str | None:
    """Pick the remote to treat as 'upstream'. Prefer a remote literally named
    'upstream'; else the configured origin."""
    remotes = _remotes(path)
    if preferred and preferred in remotes:
        return preferred
    if "upstream" in remotes:
        return "upstream"
    return remotes.get("origin")


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


def default_ref(path: str) -> str | None:
    try:
        return _git(["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], path)
    except GitError:
        return None


def _diff_count(path: str, rev: str) -> int:
    try:
        out = _git(["log", "--oneline", "-1", rev], path)
    except GitError:
        return 0
    return 1 if out else 0


def remote_ahead_count(path: str, remote: str | None) -> int:
    """Commits on remote base ref not present locally."""
    if remote is None:
        return 0
    base = f"refs/remotes/{remote}/HEAD"
    try:
        out = _git(["rev-list", "--count", f"HEAD..{base}"], path)
    except GitError:
        return 0
    return int(out) if out else 0


def recent_count(path: str, window_days: int, include_untracked: bool = True) -> int:
    """Commits on the working tree since `window_days` ago (unpushed local work)."""
    try:
        since = _git(["log", "-1", "--pretty=%H", f"--since={window_days} days ago"], path)
    except GitError:
        return 0
    if not since:
        return 0
    # count commits that have not been pushed: those not in the merge-base with origin
    base_ref = default_ref(path)
    try:
        since_sha = since.split()[0]
        if base_ref:
            # commits since base that are also not on origin HEAD
            origin = f"refs/remotes/{base_ref}/HEAD"
            out = _git(["rev-list", "--count", f"{origin}..{since_sha}"], path)
            return int(out) if out else 0
        return 1
    except GitError:
        return 1


def is_dirty(path: str, untracked: bool = True) -> bool:
    args = ["status", "--porcelain", "--untracked-files=no"]
    if untracked:
        args = ["status", "--porcelain"]
    try:
        return bool(_git(args, path))
    except GitError:
        return False


def ahead_behind(path: str, remote: str | None) -> tuple[int, int]:
    if remote is None:
        return (0, 0)
    base = f"refs/remotes/{remote}/HEAD"
    try:
        out = _git(["rev-list", "--count", f"HEAD..{base}"], path)
        ahead = int(out) if out else 0
    except GitError:
        return (0, 0)
    try:
        out = _git(["rev-list", "--count", f"{base}..HEAD"], path)
        behind = int(out) if out else 0
    except GitError:
        behind = 0
    return (ahead, behind)


def working_tree_state(path: str) -> str:
    """'clean', 'dirty', or 'dirty-with-upstream-pending'."""
    if not is_git_repo(path):
        return "not-a-repo"
    # untracked/dirty?
    if is_dirty(path):
        # is there also unpushed upstream work?
        remote = resolve_remote_name(path, None)
        if remote and remote_ahead_count(path, remote) > 0:
            return "dirty-with-upstream-pending"
        return "dirty"
    return "clean"


def stage_all(path: str) -> None:
    _git(["add", "-A"], path)


def commit(
    path: str,
    message: str,
    author_name: str | None = None,
    author_email: str | None = None,
    message_arg: str | None = None,
) -> str | None:
    """Create a commit. Uses config author identity, falling back to the
    provided overrides. Returns the commit sha, or None on failure."""
    try:
        name = author_name or _git(["config", "user.name"], path)
        email = author_email or _git(["config", "user.email"], path)
    except GitError:
        name = name or "upstream-upkeep"
        email = email or "upstream-upkeep@localhost"

    args = ["commit", "-m", message_arg or message]
    if name is not None:
        args += ["--author", f"{name} <{email}"]
    if email is not None:
        args += ["--email", email]
    res = _run(args, cwd=path, check=False)
    if res.returncode != 0:
        return None
    sha = short_sha(path)
    return sha


def push(path: str, remote: str | None, branch: str | None, message: str) -> dict:
    """Push HEAD to the remote. Returns status dict. Never auto-picks; the caller
    must have already merged upstream into the branch."""
    res = _run(["git", "push", remote, branch], cwd=path, check=False)
    return {
        "remote": remote,
        "branch": branch,
        "message": message,
        "returncode": res.returncode,
        "output": (res.stdout + res.stderr).strip(),
        "ok": res.returncode == 0,
    }


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
