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
never push, and keep the user's own commits.

To merge several branches together (e.g. three feature branches into main): list_branches, then
open_branch_set with all of them. They merge in order in one workspace; a clean merge is committed and
the next one starts; when one conflicts the set stops there. Resolve it, then continue_merge_workspace
(it commits the resolved merge and merges the rest). Repeat until workspace_status says ready, run the
tests on the combined result, then submit once for the whole set.

Submodule hubs: components_status shows each submodule's pin against its upstream, the watched files
that changed and the follow-up command to run; request_component_update raises the pin-bump request.

To test what you change: if the repo has a compose file and the user turned demo stacks on,
stack_up starts an isolated copy (random local ports, demo data, generated secrets) of the repo or of
a merge workspace. Then stack_status (running? healthy? which ports), stack_logs (with an error
summary), stack_request (send HTTP requests, e.g. seed demo data and check the API answers), and
stack_exec (run a command in a container). stack_down when done. These never touch the real install.

The user talks to you through chats too: list_chats and read_chat show what they asked or decided;
post_chat_note reports back into a chat (shown as yours). Approving requests, pushing, turning demo
stacks on, accounts and app settings are the user's, not yours."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)

server = MCPServer("upstream-manager", instructions=INSTRUCTIONS)


class AppError(ToolError):
    """A refusal or failure from the app; its message reaches the agent as the tool's error."""


def _call(method: str, path: str, body: dict | None = None, **params) -> Any:
    """One request to the app. The JSON body type is what its cross-site guard expects."""
    try:
        resp = requests.request(method, APP + path, params={k: v for k, v in params.items() if v not in (None, "")},
                                json=(body or {}) if method != "GET" else None, timeout=TIMEOUT,
                                headers={"X-Upstream-Client": "agent"})  # the app keeps some things for the user
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
def list_branches(repo: str) -> list[str]:
    """The repo's local branches (for open_branch_set; remote branches like origin/feature work too)."""
    return _call("GET", _r(repo) + "/branches")["branches"]


@server.tool(annotations=WRITE)
def open_branch_set(repo: str, branches: list[str]) -> dict:
    """Merge several branches (local, remote like origin/feature, or upstream names) into the focus branch
    together, in this order, in one workspace of its own. Stops at the first conflict for you to resolve."""
    return _call("POST", _r(repo) + "/workspaces", {"branches": branches})["workspace"]


@server.tool(annotations=WRITE)
def open_change_workspace(repo: str, title: str) -> dict:
    """A separate checkout on its own branch to make a change of your own (e.g. bump an image tag with
    its submodule pin), commit it, test it with stack_up(workspace=name), then submit it for approval."""
    return _call("POST", _r(repo) + "/workspaces", {"change": title})["workspace"]


@server.tool(annotations=WRITE)
def continue_merge_workspace(repo: str, name: str) -> dict:
    """After resolving a conflict in the workspace files: commit that merge and merge the remaining
    branches of the set. Refused while conflict markers are left."""
    return _call("POST", _r(repo) + f"/workspaces/{quote(name, safe='')}/continue", {})["workspace"]


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


# ------------------------------------------------------------------ components (submodules)
@server.tool(annotations=READ)
def components_status(repo: str) -> list[dict]:
    """A hub repo's submodules: pin vs upstream (behind / your own commits), watched files that changed
    upstream and the follow-up command the user set for after a bump."""
    return _call("GET", _r(repo) + "/components")["items"]


@server.tool(annotations=WRITE)
def fetch_components(repo: str) -> list[dict]:
    """Fetch every submodule's upstream (inside its own git dir) and return the fresh status."""
    return _call("POST", _r(repo) + "/components/fetch", {})["items"]


@server.tool(annotations=WRITE)
def request_component_update(repo: str, names: list[str] | None = None) -> dict:
    """Ask the user to approve moving submodule pins to their upstream (all behind ones, or `names`)."""
    return _call("POST", _r(repo) + "/components/request", {"names": names} if names else {})


@server.tool(annotations=WRITE)
def set_component(repo: str, name: str, enabled: bool | None = None, branch: str | None = None,
                  watch_paths: list[str] | None = None, after_update: str | None = None,
                  follow: str | None = None, tag_pattern: str | None = None) -> dict:
    """Change one submodule's settings: compare it or not, its upstream branch, the paths to watch
    (git pathspecs), the command to suggest after a bump (never run for you), and what it follows:
    "branch" (the branch head) or "releases" (the newest tag matching tag_pattern, default v*) for
    hubs that run release images pinned to each submodule's release."""
    patch = {k: v for k, v in {"enabled": enabled, "branch": branch, "watch_paths": watch_paths,
                               "after_update": after_update, "follow": follow, "tag_pattern": tag_pattern}.items()
             if v is not None}
    return _call("POST", _r(repo) + f"/components/{quote(name, safe='/')}", patch)["item"]


# ------------------------------------------------------------------ repo settings
@server.tool(annotations=WRITE)
def set_focus_branch(repo: str, branch: str) -> dict:
    """Change the branch upstreams are compared against and merged into (must be a local branch)."""
    r = _call("POST", _r(repo) + "/focus", {"branch": branch})["repo"]
    return {"focus_branch": r["focus_branch"]}


@server.tool(annotations=WRITE)
def set_upstream_enabled(repo: str, upstream: str, enabled: bool) -> str:
    """Compare against an upstream or stop comparing (it stays configured)."""
    _call("POST", _r(repo) + f"/upstream/{quote(upstream, safe='')}/enable", {"enabled": enabled})
    return f"{upstream} {'enabled' if enabled else 'disabled'}"


@server.tool(annotations=WRITE)
def add_upstream(repo: str, name: str, url: str, branch: str = "") -> dict:
    """Add an upstream remote to watch (refused if a remote of that name points elsewhere)."""
    r = _call("POST", _r(repo) + "/upstream", {"name": name, "url": url, "branch": branch or None})["repo"]
    return {"upstreams": [{k: u.get(k) for k in ("name", "url", "branch", "enabled")} for u in r.get("upstreams", [])]}


@server.tool(annotations=READ)
def workflow_health(repo: str = "") -> list[dict]:
    """GitHub Actions per repo: each workflow passing/failing/running/skipped, its latest run (link,
    branch, event) and the last 10 results."""
    out = _call("GET", "/api/workflows")
    return [r for r in out if not repo or r.get("repo_id") == repo]


# ------------------------------------------------------------------ chats
@server.tool(annotations=READ)
def list_chats(repo: str = "") -> list[dict]:
    """The user's chats (of one repo, or all; repo "_general" for chats about no repo), newest first,
    with what each is about."""
    out = _call("GET", "/api/chats")
    return [c for c in out if not repo or c.get("repo_id") == repo]


@server.tool(annotations=READ)
def read_chat(repo: str, session: str = "", last: int = 30) -> dict:
    """The messages of one chat (default: the repo's current one), most recent `last`."""
    d = _call("GET", f"/api/chat/{quote(repo, safe='')}", session=session)
    msgs = [{k: m.get(k) for k in ("role", "content", "timestamp")} for m in d.get("messages", [])][-max(1, last):]
    return {"session": d.get("session"), "title": d.get("title"), "about": d.get("tags"), "messages": msgs}


@server.tool(annotations=WRITE)
def post_chat_note(repo: str, session: str, text: str) -> str:
    """Report into one of the user's chats (shown as the coding agent's message): what you did, found
    or need from them."""
    _call("POST", _r(repo) + f"/sessions/{quote(session, safe='')}/agent-note", {"text": text})
    return "posted"


# ------------------------------------------------------------------ demo stacks
def _stack(repo: str, workspace: str) -> dict:
    return _call("GET", _r(repo) + "/stack", ws=workspace)


@server.tool(annotations=READ)
def stack_status(repo: str, workspace: str = "") -> dict:
    """The demo stack of the repo (or of a merge workspace): on or off, Docker's state, each service's
    state, health and local URL, and the last lines of the start/stop log."""
    d = _stack(repo, workspace)
    return {k: d.get(k) for k in ("enabled", "docker", "compose_file", "summary", "busy", "services", "op_log", "settings",
                                  "generated_env", "error")}


@server.tool(annotations=WRITE)
def stack_set(repo: str, env: dict | None = None, services: list[str] | None = None, profiles: list[str] | None = None,
              compose_file: str | None = None, extra_files: list[str] | None = None) -> dict:
    """Tune the demo stack: env values (merged into those already set; null removes one), only some
    services (their dependencies come along),
    compose profiles, another compose file, or override files layered on top (paths in the project,
    e.g. one that passes a proxy CA to builds). Turning stacks on is the user's."""
    if env is not None:  # merge into what is set; a null value removes that name
        current = _stack(repo, "").get("settings", {}).get("env", {})
        env = {k: v for k, v in {**current, **env}.items() if v is not None}
    patch = {k: v for k, v in {"env": env, "services": services, "profiles": profiles, "compose_file": compose_file,
                               "extra_files": extra_files}.items() if v is not None}
    return _call("POST", _r(repo) + "/stack/settings", patch)["settings"]


@server.tool(annotations=WRITE)
def stack_up(repo: str, workspace: str = "", build: bool = False, wait_seconds: int = 240) -> dict:
    """Start the isolated demo stack of the repo or of a merge workspace (to test your change). Images
    it builds are built when missing; build=True rebuilds them, after you changed their code. Waits up
    to `wait_seconds` for it to be up, then returns its status (op_log says why if it failed)."""
    import time
    _call("POST", _r(repo) + "/stack/up", {"ws": workspace or None, "build": build})
    deadline = time.time() + max(0, min(wait_seconds, 1200))
    while True:
        st = stack_status(repo, workspace)
        if not st.get("busy") or time.time() > deadline:
            return st
        time.sleep(3)


@server.tool(annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False))
def stack_down(repo: str, workspace: str = "", reset_data: bool = False) -> dict:
    """Stop the demo stack. reset_data also deletes its demo data and generated secrets."""
    return _call("POST", _r(repo) + "/stack/down", {"ws": workspace or None, "reset": reset_data})


@server.tool(annotations=READ)
def stack_logs(repo: str, service: str = "", workspace: str = "", tail: int = 200) -> dict:
    """Recent logs of the demo stack (one service or all), with a count and the last lines of anything
    that looks like an error, per service."""
    d = _call("GET", _r(repo) + "/stack/logs", ws=workspace, service=service, tail=tail)
    return {"errors": d.get("errors"), "text": d.get("text")}


@server.tool(annotations=WRITE)
def stack_request(repo: str, service: str, path: str = "/", method: str = "GET", workspace: str = "", port: int = 0,
                  headers: dict | None = None, body: Any = None) -> dict:
    """Send an HTTP request to a service of the demo stack (seed demo data, call its API, check a page).
    `port` is the container port when the service publishes more than one. JSON bodies are sent as JSON."""
    return _call("POST", _r(repo) + "/stack/request", {"ws": workspace or None, "service": service, "path": path,
                                                     "method": method, "port": port or None, "headers": headers, "body": body})


@server.tool(annotations=WRITE)
def stack_exec(repo: str, service: str, command: str, workspace: str = "", timeout: int = 120) -> dict:
    """Run a command inside a running service of the demo stack (no shell unless you call one, e.g.
    "sh -c '...'"). For CLI tools, migrations, creating demo users, reading files."""
    return _call("POST", _r(repo) + "/stack/exec", {"ws": workspace or None, "service": service, "command": command,
                                                  "timeout": timeout})


def main() -> None:
    server.run("stdio")


if __name__ == "__main__":
    main()
