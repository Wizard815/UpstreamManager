"""Upstream Manager as an MCP server, for coding agents (OpenCode, Codex, Claude Code, Hermes...).

    uv run --project /path/to/UpstreamManager upstream-mcp

It speaks MCP over stdio and is a thin client of the running app's HTTP API
(UPSTREAM_APP_URL, default http://127.0.0.1:5000), so the agent sees the same
repos, memory and requests as the web page, and the same rules hold. It can
read, fetch, take notes, open merge workspaces and raise requests; it has no
tool that approves, merges into the focus branch or pushes. Those stay with you.
"""
from __future__ import annotations

import os
from typing import Any
from urllib.parse import quote

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

APP = os.environ.get("UPSTREAM_APP_URL", "http://127.0.0.1:5000").rstrip("/")
TIMEOUT = int(os.environ.get("UPSTREAM_MCP_TIMEOUT", "300"))

INSTRUCTIONS = """Upstream Manager keeps the user's local git repos in sync with their upstreams.
Start with list_repos, then repo_briefing for the repo you work on: it has the branches, what each
upstream would bring, the user's notes and preferences, recent day logs and the rules.

To bring in upstream changes that conflict: call conflict_details to see both sides of each file,
then open_merge_workspace. It gives a separate checkout (path) on its own branch with the merge
started; resolve the conflicts there with your own file and shell tools, run the project's tests,
`git add` and `git commit`. Check workspace_status, then submit_merge_workspace with a short summary.
That raises an approval request; the user decides. Never edit the repo's main working tree to merge,
never push, and keep the user's own commits."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

server = MCPServer("upstream-manager", instructions=INSTRUCTIONS)


class AppError(ToolError):
    """A refusal or failure from the app; its message reaches the agent as the tool's error."""


def _call(method: str, path: str, body: dict | None = None, **params) -> Any:
    """One request to the app. The JSON body type is what its cross-site guard expects."""
    try:
        resp = requests.request(method, APP + path, params={k: v for k, v in params.items() if v not in (None, "")},
                                json=(body or {}) if method != "GET" else None, timeout=TIMEOUT)
    except requests.ConnectionError:
        raise AppError(f"Upstream Manager is not running at {APP} (start it, or set UPSTREAM_APP_URL)")
    if resp.status_code == 404:
        raise AppError(f"not found: {path} (check the repo id with list_repos, or the name with workspace_status)")
    try:
        data = resp.json()
    except ValueError:
        raise AppError(f"{method} {path}: HTTP {resp.status_code}")
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        raise AppError((data.get("error") if isinstance(data, dict) else None) or f"{method} {path}: HTTP {resp.status_code}")
    return data


def _r(repo: str) -> str:
    return f"/api/repo/{quote(repo, safe='')}"


# ------------------------------------------------------------------ reading
@server.tool(annotations=READ)
def list_repos() -> list[dict]:
    """Every watched repo: id, path, focus branch, and how far behind/ahead of each upstream it is."""
    out = []
    for r in _call("GET", "/api/repos"):
        out.append({"id": r["id"], "name": r.get("name"), "path": r.get("path"), "focus_branch": r.get("focus_branch"),
                    "uncommitted_files": r.get("dirty"), "error": r.get("error"),
                    "upstreams": [{k: u.get(k) for k in ("name", "branch", "behind", "ahead", "conflict", "missing_ref",
                                                          "conflict_detail")} for u in r.get("upstream_details") or []]})
    return out


@server.tool(annotations=READ)
def repo_briefing(repo: str) -> str:
    """Markdown briefing on one repo: branches, upstreams, what they bring and whether they conflict,
    the user's notes and preferences, recent day logs, open requests, merge workspaces and the rules."""
    return _call("GET", _r(repo) + "/briefing")["text"]


@server.tool(annotations=READ)
def list_commits(repo: str, view: str = "behind", upstream: str = "", query: str = "", limit: int = 30) -> list[dict]:
    """Commits of a repo. view: "behind" (upstream has, you lack), "ahead" (only yours) or "all"
    (focus branch history). query filters by message."""
    return _call("GET", _r(repo) + "/commits", view=view, upstream=upstream, q=query, limit=limit)["items"]


@server.tool(annotations=READ)
def show_commit(repo: str, sha: str) -> dict:
    """One commit: message, author, file stat and patch (long patches are cut)."""
    return _call("GET", _r(repo) + f"/commit/{quote(sha, safe='')}")


@server.tool(annotations=READ)
def pull_report(repo: str, upstream: str) -> dict:
    """Which incoming upstream commits merge cleanly and which touch conflicting files, and the
    last commit up to which a pull has no conflicts."""
    return _call("GET", _r(repo) + "/pull-report", upstream=upstream)


@server.tool(annotations=READ)
def conflict_details(repo: str, upstream: str) -> dict:
    """For each file a merge of `upstream` would conflict in: what the user's side and the upstream
    side each changed since they split (diffs), and the commits that did it."""
    return _call("GET", _r(repo) + "/conflicts", upstream=upstream)


@server.tool(annotations=READ)
def search_memory(query: str, repo: str = "", all_repos: bool = False) -> list[dict]:
    """Search what Upstream Manager remembers: notes, preferences, decisions, events, indexed commits
    and day logs. Mention file names or commit ids for exact matches. all_repos searches every repo."""
    if not repo:
        repos = _call("GET", "/api/repos")
        if not repos:
            return []
        repo, all_repos = repos[0]["id"], True
    return _call("GET", _r(repo) + "/memory", q=query, scope="all" if all_repos else "repo")["items"]


@server.tool(annotations=READ)
def day_logs(repo: str, limit: int = 5) -> list[dict]:
    """The newest day logs of a repo: what was worked on, decided and left open each day."""
    return _call("GET", _r(repo) + "/memory", kind="daylog")["items"][:limit]


@server.tool(annotations=READ)
def list_requests(repo: str = "", pending_only: bool = True) -> list[dict]:
    """Approval requests (merges, pushes, pin bumps, Actions changes) and their status."""
    out = _call("GET", "/api/requests")
    return [r for r in out if (not repo or r.get("repo_id") == repo) and (not pending_only or r.get("status") == "pending")]


# ------------------------------------------------------------------ acting
@server.tool(annotations=WRITE)
def fetch_upstreams(repo: str) -> dict:
    """Fetch every upstream of a repo and return the fresh comparison. Changes no branch."""
    return _call("POST", _r(repo) + "/fetch", {})


@server.tool(annotations=WRITE)
def add_note(repo: str, text: str) -> str:
    """Remember a note about a repo (a decision, a gotcha, a follow-up). The user sees it on the Memory tab."""
    _call("POST", _r(repo) + "/memory", {"text": f"[agent] {text}"})
    return "noted"


@server.tool(annotations=WRITE)
def request_clean_pull(repo: str, upstream: str, target: str) -> dict:
    """Ask the user to approve pulling upstream commits up to `target` (a commit id from pull_report's
    clean part, which merges without conflicts). Nothing merges until they approve."""
    return _call("POST", _r(repo) + f"/upstream/{quote(upstream, safe='')}/request-pull", {"target": target})["request"]


@server.tool(annotations=WRITE)
def open_merge_workspace(repo: str, upstream: str) -> dict:
    """Start merging `upstream` into the focus branch in a separate checkout of its own (`path`), with
    conflicts left in the files. Work there with your own tools; the repo's own tree is not touched.
    Returns the existing workspace if one is open for the same upstream commit."""
    return _call("POST", _r(repo) + "/workspaces", {"upstream": upstream})["workspace"]


@server.tool(annotations=READ)
def workspace_status(repo: str, name: str = "") -> Any:
    """A merge workspace's state and next step (or every workspace of the repo when name is empty)."""
    if not name:
        return _call("GET", _r(repo) + "/workspaces")["items"]
    return _call("GET", _r(repo) + f"/workspaces/{quote(name, safe='')}")["workspace"]


@server.tool(annotations=WRITE)
def submit_merge_workspace(repo: str, name: str, summary: str) -> dict:
    """Raise an approval request to bring a finished workspace into the focus branch. The merge must be
    committed with no conflict markers left. `summary`: how the conflicts were resolved and what was tested."""
    return _call("POST", _r(repo) + f"/workspaces/{quote(name, safe='')}/submit", {"summary": summary})["request"]


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def discard_merge_workspace(repo: str, name: str) -> str:
    """Delete a merge workspace and its branch (its uncommitted and committed work is lost)."""
    _call("DELETE", _r(repo) + f"/workspaces/{quote(name, safe='')}")
    return "discarded"


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
