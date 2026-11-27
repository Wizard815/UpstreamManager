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
from typing import Annotated, Any, Literal
from urllib.parse import quote

import requests
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

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
never push, and keep the user's own commits. Stay inside the workspace: never change another repo, an
upstream, the user's checkout or any git config outside it. To change code in a git submodule, commit
inside the submodule within the workspace, then commit the new pin in the repo: on approval Upstream
Manager brings that submodule commit into the user's checkout (an auto-approval check fetches it from
the workspace too). Never publish it somewhere to make that work.

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

To run a build or tests somewhere clean, two places, both with what is pushed: try_in_sandbox runs it in a
throwaway container on Studio's machine (any distro, or offline), and forge_job_run runs it on the home forge's
runners like CI (runner ubuntu-latest, or gpu for GPU work), followed by forge_job_status. You decide when either
helps; say which you used.

Working alongside other agents: call overseer_plan first. The Overseer (plain rules, not a model) ranks repos
and tasks by what matters now. next_task(agent=<your name>) hands you the most important open task nobody has,
claimed for you; check in at least every 30 minutes (heartbeat_task / comment_task / update_task) or the claim
runs out and someone else may take it; release_task when done (after submitting), open or parked. Talk on a
task with comment_task, or to any agent with send_message ("*" for all, "user" for the user); read_messages
when you start and between steps. An agent started by cron: next_task, do it, release, repeat until nothing is left.

What protects the user's branch, which you should know about: requests may approve themselves when the user's
check command passes (auto-approval rules; repo_briefing says which); merge workspaces reuse earlier conflict
resolutions (rerere) and may merge with Mergiraf; a release wait means only upstream commits that are N days
old are taken (the newer ones show as "waiting"). Repo groups (list_groups, fetch_group) and GitHub issues
(list_issues, import_issues) organise work. The user sets auto-approval, release waits, notifications and the
Overseer; agents can't.

The user talks to you through chats too: list_chats and read_chat show what they asked or decided;
post_chat_note reports back into a chat (shown as yours). Approving requests, pushing, turning demo
stacks on, accounts and app settings are the user's, not yours."""

READ = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False)

# Parameter types shared by the tools, so every tool describes and checks them the same way.
# min_length=1 matters: an empty repo or workspace name otherwise builds a path like /workspaces/.
Repo = Annotated[str, Field(min_length=1, description="Repo id from list_repos.")]
ChatRepo = Annotated[str, Field(min_length=1, description='Repo id from list_repos, or "_general" for chats about no repo.')]
Upstream = Annotated[str, Field(min_length=1, description="Upstream (remote) name as list_repos shows it, e.g. upstream.")]
WorkspaceName = Annotated[str, Field(min_length=1, description="Merge workspace name from open_merge_workspace or workspace_status.")]
StackTarget = Annotated[str, Field(description="A merge workspace name to use its own stack; empty for the repo's.")]
Service = Annotated[str, Field(min_length=1, description="Compose service name, as stack_status lists it.")]
Sha = Annotated[str, Field(min_length=4, description="Commit id (full or abbreviated) from list_commits or pull_report.")]


def _text(description: str, max_length: int = 4000):
    return Annotated[str, Field(min_length=1, max_length=max_length, pattern=r"\S", description=description)]


class StrictServer(MCPServer):
    """The SDK drops argument names a tool doesn't take, so a typo such as "repoo" went unnoticed and the
    call ran without it. Refuse them instead, and say so in the published schemas."""

    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema = {**tool.input_schema, "additionalProperties": False}
        return tools

    async def call_tool(self, name, arguments, context=None):
        tool = next((t for t in await super().list_tools() if t.name == name), None)
        if tool is not None:
            known = set(tool.input_schema.get("properties") or {})
            unknown = sorted(set(arguments or {}) - known)
            if unknown:
                raise ToolError(f"{name} does not take {', '.join(unknown)}; it takes {', '.join(sorted(known)) or 'no arguments'}")
        return await super().call_tool(name, arguments, context)


server = StrictServer("upstream-manager", instructions=INSTRUCTIONS)


class AppError(ToolError):
    """A refusal or failure from the app; its message reaches the agent as the tool's error."""


def _call(method: str, endpoint: str, body: dict | None = None, timeout: float | None = None, /, **params) -> Any:
    """One request to the app. The JSON body type is what its cross-site guard expects.
    Its own arguments are positional-only, so a query parameter may be called `path` (list_references)."""
    try:
        resp = requests.request(method, APP + endpoint, params={k: v for k, v in params.items() if v not in (None, "")},
                                json=(body or {}) if method != "GET" else None, timeout=timeout or TIMEOUT,
                                headers={"X-Upstream-Client": "agent"})  # the app keeps some things for the user
    except requests.ConnectionError:
        raise AppError(f"Upstream Manager is not running at {APP} (start it, or set UPSTREAM_APP_URL)")
    if resp.status_code == 404:
        raise AppError(f"not found: {endpoint} (check the repo id with list_repos, or the name with workspace_status)")
    try:
        data = resp.json()
    except ValueError:
        raise AppError(f"{method} {endpoint}: HTTP {resp.status_code}")
    if resp.status_code >= 400 or (isinstance(data, dict) and data.get("ok") is False):
        raise AppError((data.get("error") if isinstance(data, dict) else None) or f"{method} {endpoint}: HTTP {resp.status_code}")
    return data


def _r(repo: str) -> str:
    return f"/api/repo/{quote(repo, safe='')}"


# ------------------------------------------------------------------ reading
@server.tool(annotations=READ)
def list_repos(group: Annotated[str, Field(description="Only repos in this group (see list_groups); empty for all.")] = "") -> list[dict]:
    """Every watched repo: id, path, focus branch, groups, and how far behind/ahead of each upstream it is."""
    out = []
    for r in _call("GET", "/api/repos", group=group):
        out.append({"id": r["id"], "name": r.get("name"), "path": r.get("path"), "focus_branch": r.get("focus_branch"),
                    "groups": r.get("groups") or [], "uncommitted_files": r.get("dirty"), "error": r.get("error"),
                    "upstreams": [{k: u.get(k) for k in ("name", "branch", "behind", "ahead", "conflict", "missing_ref",
                                                          "conflict_detail")} for u in r.get("upstream_details") or []]})
    return out


# ------------------------------------------------------------------ the Overseer, the board, other agents
TaskId = Annotated[str, Field(min_length=4, description="Task id from list_tasks or create_task.")]
AgentName = Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._:@/-]{0,63}$",
                                 description='Your agent name, the same every call (e.g. "claude-1", "codex-laptop"). '
                                             'Others see it on the tasks you hold and in messages.')]


@server.tool(annotations=READ)
def overseer_plan(agent: Annotated[str, Field(description="Your agent name (to count your unread messages); optional.")] = "") -> str:
    """Start here. What matters now: repos most in need of work and why, the open tasks in order, who works on
    what, and how agents share work here. The Overseer (plain rules) keeps this up to date every few minutes."""
    return _call("GET", "/api/overseer/plan", agent=agent)["text"]


@server.tool(annotations=WRITE)
def next_task(agent: AgentName,
              repo: Annotated[str, Field(description="Only this repo; empty for any.")] = "",
              group: Annotated[str, Field(description="Only repos in this group; empty for any.")] = "",
              kinds: Annotated[list[Literal["fix", "bug", "feature", "merge", "chore"]], Field(description="Only these kinds; empty for any.")] = []) -> dict:
    """Get the most important open task nobody has, already claimed for you, with a brief of what to do.
    If you still hold one, you get that one back. {task: null} means nothing for you now. Agents run by cron:
    call this first, do the task, release_task, repeat until it says nothing is left."""
    return _call("POST", "/api/overseer/next", {"agent": agent, "repo": repo, "group": group, "kinds": kinds})


@server.tool(annotations=READ)
def board_view(repo: Annotated[str, Field(description="Only this repo; empty for all.")] = "",
               group: Annotated[str, Field(description="Only this group; empty for all.")] = "") -> dict:
    """The kanban: open / working / parked / done, each column most important first, with who holds each card."""
    b = _call("GET", "/api/board", repo=repo, group=group)
    slim = lambda c: {k: c.get(k) for k in ("id", "repo_id", "title", "kind", "priority", "claimed_by", "comments")}
    return {"columns": {k: [slim(c) for c in v] for k, v in b["columns"].items()}, "agents": b["agents"]}


@server.tool(annotations=WRITE)
def claim_task(task: TaskId, agent: AgentName,
               minutes: Annotated[int, Field(ge=5, le=480, description="How long before it runs out without a check-in.")] = 60) -> dict:
    """Take a specific task (next_task usually picks for you). Refused when another agent holds it: then message them or pick another."""
    return _call("POST", f"/api/tasks/{quote(task, safe='')}/claim", {"agent": agent, "minutes": minutes})["task"]


@server.tool(annotations=WRITE)
def heartbeat_task(task: TaskId, agent: AgentName,
                   note: Annotated[str, Field(max_length=2000, description="Progress, for the task's log (optional).")] = "") -> dict:
    """Still on it: keeps your claim alive (do it at least every 30 minutes), with an optional progress note."""
    t = _call("POST", f"/api/tasks/{quote(task, safe='')}/heartbeat", {"agent": agent, "note": note or None})["task"]
    return {"id": t["id"], "claimed_by": t.get("claimed_by"), "status": t["status"]}


@server.tool(annotations=WRITE)
def release_task(task: TaskId, agent: AgentName,
                 status: Annotated[Literal["done", "open", "parked"], Field(description="done: finished and submitted; open: someone else can carry on; parked: blocked (say why).")],
                 note: Annotated[str, Field(max_length=2000, description="What you did, or what blocks it.")] = "") -> dict:
    """Let go of a task you hold."""
    t = _call("POST", f"/api/tasks/{quote(task, safe='')}/release", {"agent": agent, "status": status, "note": note or None})["task"]
    return {"id": t["id"], "status": t["status"]}


@server.tool(annotations=WRITE)
def comment_task(task: TaskId, agent: AgentName,
                 text: _text("The comment: a finding, a handoff, a question.", 4000),
                 to: Annotated[str, Field(description='Another agent\'s name (it lands in their messages too), "user" for the user, or empty.')] = "") -> dict:
    """Say something on a task's thread; everyone working on it reads it there."""
    return _call("POST", f"/api/tasks/{quote(task, safe='')}/comments", {"agent": agent, "text": text, "to": to or None})["comment"]


@server.tool(annotations=WRITE)
def send_message(agent: AgentName,
                 to: Annotated[str, Field(min_length=1, description='An agent\'s name, "*" for every agent, or "user".')],
                 text: _text("The message.", 4000),
                 task: Annotated[str, Field(description="A task it is about (optional).")] = "") -> dict:
    """Message another agent (or all, or the user) about anything: "I'm changing the API in x, hold off", "who has the db migration?"."""
    return _call("POST", "/api/messages", {"agent": agent, "to": to, "text": text, "task": task or None})["message"]


@server.tool(annotations=WRITE)
def read_messages(agent: AgentName,
                  all_messages: Annotated[bool, Field(description="Also ones you already read.")] = False) -> list[dict]:
    """Your messages (and ones to every agent), oldest first; they count as read after this."""
    return _call("GET", "/api/messages", agent=agent, **({"all": "1"} if all_messages else {}))["items"]


@server.tool(annotations=READ)
def list_agents() -> list[dict]:
    """Agents that used the board lately: what each holds and how long since it was heard from."""
    return _call("GET", "/api/agents")["items"]


@server.tool(annotations=WRITE)
def set_task_priority(task: TaskId, agent: AgentName,
                      priority: Annotated[Literal["urgent", "high", "normal", "low"], Field(description="urgent only for breakage that blocks users.")]) -> dict:
    """Raise or lower a task's priority (it is logged with your name)."""
    t = _call("POST", f"/api/tasks/{quote(task, safe='')}/priority", {"agent": agent, "priority": priority})["task"]
    return {"id": t["id"], "priority": t["priority"]}


@server.tool(annotations=READ)
def list_issues(repo: Repo,
                state: Annotated[Literal["open", "closed", "all"], Field(description="Which issues.")] = "open",
                remote: Annotated[str, Field(description="Which github.com or home-forge remote's issues (default: origin, else the first upstream).")] = "") -> dict:
    """The GitHub or home-forge issues of a repo (not pull requests), newest activity first, each with the task it became if imported."""
    return _call("GET", _r(repo) + "/issues", state=state, remote=remote)


@server.tool(annotations=WRITE)
def import_issues(repo: Repo,
                  numbers: Annotated[list[int], Field(min_length=1, max_length=50, description="Issue numbers from list_issues.")],
                  remote: Annotated[str, Field(description="The remote list_issues used, if not the default.")] = "") -> dict:
    """Make Work tasks of GitHub or home-forge issues (title, text and a link back). An issue already imported is skipped."""
    return _call("POST", _r(repo) + "/issues/import", {"numbers": numbers, "remote": remote})


@server.tool(annotations=READ)
def list_groups() -> dict:
    """The user's repo groups (e.g. "glance": the Glance apps) and the repo ids in each."""
    return _call("GET", "/api/groups")["groups"]


@server.tool(annotations=WRITE)
def set_repo_groups(repo: Repo, groups: Annotated[list[str], Field(description='Every group the repo belongs to, e.g. ["glance", "web"]; [] for none. Lower-case words.')]) -> list[str]:
    """Put a repo in groups (replaces its list), so it can be listed, fetched and checked with the rest of its group."""
    return _call("POST", _r(repo) + "/groups", {"groups": groups})["groups"]


@server.tool(annotations=WRITE)
def fetch_group(group: Annotated[str, Field(min_length=1, description="Group name from list_groups.")]) -> dict:
    """Fetch the upstreams of every repo in a group; returns what each would bring and any conflicts."""
    return _call("POST", f"/api/groups/{quote(group, safe='')}/fetch", {})


@server.tool(annotations=READ)
def repo_briefing(repo: Repo) -> str:
    """Markdown briefing on one repo: branches, upstreams, what they bring and whether they conflict,
    the user's notes and preferences, recent day logs, open requests, merge workspaces and the rules."""
    return _call("GET", _r(repo) + "/briefing")["text"]


@server.tool(annotations=READ)
def list_commits(
    repo: Repo,
    view: Annotated[Literal["behind", "ahead", "all"], Field(
        description='"behind": commits the upstream has that you lack; "ahead": only your own; "all": the focus branch history.')] = "behind",
    upstream: Annotated[str, Field(description="For behind/ahead: which upstream (name from list_repos); empty for the first enabled one that is not origin.")] = "",
    query: Annotated[str, Field(description="Only commits whose message contains this.")] = "",
    limit: Annotated[int, Field(ge=1, le=200, description="At most this many commits (1-200).")] = 30,
) -> list[dict]:
    """Commits of a repo: what an upstream would bring (behind), your own (ahead) or the whole focus branch (all)."""
    return _call("GET", _r(repo) + "/commits", view=view, upstream=upstream, q=query, limit=limit)["items"]


@server.tool(annotations=READ)
def show_commit(repo: Repo, sha: Sha) -> dict:
    """One commit: message, author, file stat and patch (long patches are cut)."""
    return _call("GET", _r(repo) + f"/commit/{quote(sha, safe='')}")


@server.tool(annotations=READ)
def pull_report(repo: Repo, upstream: Upstream) -> dict:
    """Which incoming upstream commits merge cleanly and which touch conflicting files, and the
    last commit up to which a pull has no conflicts."""
    return _call("GET", _r(repo) + "/pull-report", upstream=upstream)


@server.tool(annotations=READ)
def conflict_details(repo: Repo, upstream: Upstream) -> dict:
    """For each file a merge of `upstream` would conflict in: what the user's side and the upstream
    side each changed since they split (diffs), and the commits that did it."""
    return _call("GET", _r(repo) + "/conflicts", upstream=upstream)


@server.tool(annotations=READ)
def search_memory(
    query: _text("What to look for. File names and commit ids match exactly.", 500),
    repo: Annotated[str, Field(description="Repo id to search; empty searches every repo.")] = "",
    all_repos: Annotated[bool, Field(description="Search every repo, not only `repo`.")] = False,
) -> list[dict]:
    """Search what Upstream Manager remembers: notes, preferences, decisions, events, indexed commits
    and day logs."""
    if not repo:
        repos = _call("GET", "/api/repos")
        if not repos:
            return []
        repo, all_repos = repos[0]["id"], True
    return _call("GET", _r(repo) + "/memory", q=query, scope="all" if all_repos else "repo")["items"]


@server.tool(annotations=READ)
def day_logs(repo: Repo, limit: Annotated[int, Field(ge=1, le=50, description="How many days, newest first (1-50).")] = 5) -> list[dict]:
    """The newest day logs of a repo: what was worked on, decided and left open each day."""
    return _call("GET", _r(repo) + "/memory", kind="daylog")["items"][:limit]


@server.tool(annotations=READ)
def list_requests(
    repo: Annotated[str, Field(description="Only this repo's requests (id from list_repos); empty for every repo.")] = "",
    pending_only: Annotated[bool, Field(description="Only requests still waiting for the user. Default true.")] = True,
) -> list[dict]:
    """Approval requests (merges, pushes, pin bumps, Actions changes) and their status."""
    out = _call("GET", "/api/requests")
    return [r for r in out if (not repo or r.get("repo_id") == repo) and (not pending_only or r.get("status") == "pending")]


# ------------------------------------------------------------------ acting
@server.tool(annotations=WRITE)
def fetch_upstreams(repo: Repo) -> dict:
    """Fetch every upstream of a repo and return the fresh comparison. Changes no branch."""
    return _call("POST", _r(repo) + "/fetch", {})


@server.tool(annotations=WRITE)
def add_note(repo: Repo, text: _text("The note: a decision, a gotcha or a follow-up, in a sentence or two.")) -> str:
    """Remember a note about a repo (a decision, a gotcha, a follow-up). The user sees it on the Memory tab."""
    _call("POST", _r(repo) + "/memory", {"text": f"[agent] {text}"})
    return "noted"


@server.tool(annotations=WRITE)
def request_clean_pull(
    repo: Repo, upstream: Upstream,
    target: Annotated[str, Field(min_length=4, description="Commit id from pull_report's clean part: the pull stops there.")],
) -> dict:
    """Ask the user to approve pulling upstream commits up to `target` (a commit id from pull_report's
    clean part, which merges without conflicts). Nothing merges until they approve."""
    return _call("POST", _r(repo) + f"/upstream/{quote(upstream, safe='')}/request-pull", {"target": target})["request"]


@server.tool(annotations=WRITE)
def open_merge_workspace(repo: Repo, upstream: Upstream) -> dict:
    """Start merging `upstream` into the focus branch in a separate checkout of its own (`path`), with
    conflicts left in the files. Work there with your own tools; the repo's own tree is not touched.
    Returns the existing workspace if one is open for the same upstream commit."""
    return _call("POST", _r(repo) + "/workspaces", {"upstream": upstream})["workspace"]


@server.tool(annotations=READ)
def list_branches(repo: Repo) -> list[str]:
    """The repo's local branches (for open_branch_set; remote branches like origin/feature work too)."""
    return _call("GET", _r(repo) + "/branches")["branches"]


@server.tool(annotations=WRITE)
def open_branch_set(
    repo: Repo,
    branches: Annotated[list[Annotated[str, Field(min_length=1)]], Field(
        min_length=1, description="Branches to merge, in this order: local, remote (origin/feature) or upstream names.")],
) -> dict:
    """Merge several branches (local, remote like origin/feature, or upstream names) into the focus branch
    together, in this order, in one workspace of its own. Stops at the first conflict for you to resolve."""
    return _call("POST", _r(repo) + "/workspaces", {"branches": branches})["workspace"]


@server.tool(annotations=WRITE)
def open_change_workspace(repo: Repo, title: _text("Short title of the change; it names the workspace branch.", 200),
                          task: Annotated[str, Field(description="The board task this work is for (from next_task); links them, "
                                                                 "so the Overseer can tell when it is ready but not submitted.")] = "") -> dict:
    """A separate checkout on its own branch to make a change of your own (e.g. bump an image tag with
    its submodule pin), commit it, test it with stack_up(workspace=name), then submit it for approval."""
    return _call("POST", _r(repo) + "/workspaces", {"change": title, **({"task": task} if task else {})})["workspace"]


@server.tool(annotations=WRITE)
def continue_merge_workspace(repo: Repo, name: WorkspaceName) -> dict:
    """After resolving a conflict in the workspace files: commit that merge and merge the remaining
    branches of the set. Refused while conflict markers are left."""
    return _call("POST", _r(repo) + f"/workspaces/{quote(name, safe='')}/continue", {})["workspace"]


@server.tool(annotations=READ)
def workspace_status(
    repo: Repo,
    name: Annotated[str, Field(description="One workspace's name; empty for every workspace of the repo.")] = "",
) -> Any:
    """A merge workspace's state and next step (or every workspace of the repo when name is empty)."""
    if not name:
        return _call("GET", _r(repo) + "/workspaces")["items"]
    return _call("GET", _r(repo) + f"/workspaces/{quote(name, safe='')}")["workspace"]


@server.tool(annotations=WRITE)
def submit_merge_workspace(
    repo: Repo, name: WorkspaceName,
    summary: _text("How the conflicts were resolved and what was tested; the user reads it before approving."),
) -> dict:
    """Raise an approval request to bring a finished workspace into the focus branch. The merge must be
    committed with no conflict markers left."""
    return _call("POST", _r(repo) + f"/workspaces/{quote(name, safe='')}/submit", {"summary": summary})["request"]


@server.tool(annotations=DESTRUCTIVE)
def discard_merge_workspace(repo: Repo, name: WorkspaceName) -> str:
    """Delete a merge workspace and its branch (its uncommitted and committed work is lost)."""
    _call("DELETE", _r(repo) + f"/workspaces/{quote(name, safe='')}")
    return "discarded"


# ------------------------------------------------------------------ components (submodules)
@server.tool(annotations=READ)
def components_status(repo: Repo) -> list[dict]:
    """A hub repo's submodules: pin vs upstream (behind / your own commits), watched files that changed
    upstream and the follow-up command the user set for after a bump."""
    return _call("GET", _r(repo) + "/components")["items"]


@server.tool(annotations=WRITE)
def fetch_components(repo: Repo) -> list[dict]:
    """Fetch every submodule's upstream (inside its own git dir) and return the fresh status."""
    return _call("POST", _r(repo) + "/components/fetch", {})["items"]


@server.tool(annotations=WRITE)
def request_component_update(
    repo: Repo,
    names: Annotated[list[str] | None, Field(description="Only these submodules (names from components_status); omit for every one that is behind.")] = None,
) -> dict:
    """Ask the user to approve moving submodule pins to their upstream (all behind ones, or `names`)."""
    return _call("POST", _r(repo) + "/components/request", {"names": names} if names else {})


@server.tool(annotations=WRITE)
def set_component(
    repo: Repo,
    name: Annotated[str, Field(min_length=1, description="Submodule name from components_status.")],
    enabled: Annotated[bool | None, Field(description="Compare this submodule against its upstream, or not.")] = None,
    branch: Annotated[str | None, Field(description="The upstream branch it follows.")] = None,
    watch_paths: Annotated[list[str] | None, Field(description="Git pathspecs whose upstream changes are flagged.")] = None,
    after_update: Annotated[str | None, Field(description="Command to suggest after a bump (never run for you).")] = None,
    follow: Annotated[Literal["branch", "releases"] | None, Field(
        description='"branch": the branch head; "releases": the newest tag matching tag_pattern.')] = None,
    tag_pattern: Annotated[str | None, Field(description="Glob for release tags when follow is releases (default v*).")] = None,
) -> dict:
    """Change one submodule's settings: compare it or not, its upstream branch, the paths to watch,
    the command to suggest after a bump, and what it follows (the branch head, or release tags for hubs
    that run release images pinned to each submodule's release). Only the fields you pass change."""
    patch = {k: v for k, v in {"enabled": enabled, "branch": branch, "watch_paths": watch_paths,
                               "after_update": after_update, "follow": follow, "tag_pattern": tag_pattern}.items()
             if v is not None}
    return _call("POST", _r(repo) + f"/components/{quote(name, safe='/')}", patch)["item"]


# ------------------------------------------------------------------ repo settings
@server.tool(annotations=WRITE)
def set_focus_branch(repo: Repo, branch: Annotated[str, Field(min_length=1, description="A local branch name, from list_branches.")]) -> dict:
    """Change the branch upstreams are compared against and merged into (must be a local branch)."""
    r = _call("POST", _r(repo) + "/focus", {"branch": branch})["repo"]
    return {"focus_branch": r["focus_branch"]}


@server.tool(annotations=WRITE)
def set_upstream_enabled(
    repo: Repo, upstream: Upstream,
    enabled: Annotated[bool, Field(description="true to compare against it, false to stop (it stays configured).")],
) -> str:
    """Compare against an upstream or stop comparing (it stays configured)."""
    _call("POST", _r(repo) + f"/upstream/{quote(upstream, safe='')}/enable", {"enabled": enabled})
    return f"{upstream} {'enabled' if enabled else 'disabled'}"


@server.tool(annotations=WRITE)
def add_upstream(
    repo: Repo,
    name: Annotated[str, Field(min_length=1, description="Name for the remote, e.g. upstream.")],
    url: Annotated[str, Field(min_length=1, description="Git URL of the upstream repo.")],
    branch: Annotated[str, Field(description="Branch to follow; empty for the remote's default branch.")] = "",
) -> dict:
    """Add an upstream remote to watch (refused if a remote of that name points elsewhere)."""
    r = _call("POST", _r(repo) + "/upstream", {"name": name, "url": url, "branch": branch or None})["repo"]
    return {"upstreams": [{k: u.get(k) for k in ("name", "url", "branch", "enabled")} for u in r.get("upstreams", [])]}


@server.tool(annotations=READ)
def workflow_health(repo: Annotated[str, Field(description="Only this repo (id from list_repos); empty for every repo.")] = "") -> list[dict]:
    """CI per repo (GitHub Actions, or Forgejo Actions when the home remote is the forge): each workflow passing/failing/running/skipped, its latest run (link,
    branch, event) and the last 10 results."""
    out = _call("GET", "/api/workflows")
    return [r for r in out if not repo or r.get("repo_id") == repo]


# ------------------------------------------------------------------ the home forge
@server.tool(annotations=READ)
def forge_repos(query: Annotated[str, Field(description="Words in the repo's name; empty for all the token can see.")] = "") -> list[dict]:
    """Repos on the user's home forge (Forgejo/Gitea): name, private, template, mirror, fork."""
    return _call("GET", "/api/forge/repos", q=query)["repos"]


@server.tool(annotations=WRITE)
def forge_new_repo(base: Annotated[str, Field(min_length=3, description="OWNER/REPO on the forge, or a git URL such as https://github.com/owner/repo.")],
                   mode: Annotated[Literal["copy", "fork", "template"], Field(description="copy: new private forge repo with the base's history, base kept as upstream "
                                                                                     "(works for GitHub bases); fork: the forge's own fork of a forge repo; "
                                                                                     "template: a forge template repo's files with fresh history.")] = "copy",
                   name: Annotated[str, Field(description="Name of the new repo; empty for the base's name.")] = "") -> dict:
    """Start a private repo on the home forge from a base, clone it here and watch it (home: the new forge repo).
    Only when the user asked for a new repo."""
    return _call("POST", "/api/forge/new", {"base": base, "mode": mode, "name": name}, 1900)


# ------------------------------------------------------------------ test somewhere clean
@server.tool(annotations=READ)
def forge_runners() -> dict:
    """The home forge's runners and their labels (e.g. ubuntu-latest, gpu). Needs an admin forge token; without one, ask the user."""
    return _call("GET", "/api/forge/runners")


@server.tool(annotations=WRITE)
def forge_job_run(command: Annotated[str, Field(min_length=1, description="Shell command to run, e.g. 'pytest -q' or 'nvidia-smi'.")],
                  runner: Annotated[str, Field(description="Runner label: ubuntu-latest for normal builds and tests, gpu (or the user's GPU label) for GPU work.")] = "ubuntu-latest",
                  repo: Annotated[str, Field(description="A repo id from list_repos to check out (its forge copy, at its current branch unless ref); empty for none.")] = "",
                  image: Annotated[str, Field(description="Container image to run in, e.g. node:24-bookworm; empty for the runner's own.")] = "",
                  ref: Annotated[str, Field(description="Branch, tag or commit of repo.")] = "") -> dict:
    """Run one command on the home forge's own runners, like CI, and return its run id. Uses what is pushed to the forge.
    Follow it with forge_job_status. Jobs run real code on the user's machines: only run what the task needs."""
    body = {"command": command, "runner": runner, "image": image, "ref": ref}
    return _call("POST", _r(repo) + "/forge/job" if repo else "/api/forge/jobs", body)


@server.tool(annotations=READ)
def forge_job_status(run_id: Annotated[int, Field(description="The run id forge_job_run returned.")],
                     logs: Annotated[int, Field(ge=0, le=500, description="Output lines to return once it is done.")] = 80) -> dict:
    """A forge job's status (waiting = no idle runner with that label yet, running, success, failure, cancelled) and, when done, its output."""
    return _call("GET", f"/api/forge/jobs/{int(run_id)}", logs=logs)


@server.tool(annotations=WRITE)
def forge_job_cancel(run_id: Annotated[int, Field(description="The run id forge_job_run returned.")]) -> dict:
    """Stop a forge job that is waiting or running (e.g. one you started by mistake)."""
    return _call("POST", f"/api/forge/jobs/{int(run_id)}/cancel", {})


@server.tool(annotations=WRITE)
def try_in_sandbox(repo: Repo,
                   command: Annotated[str, Field(min_length=1, description="Run in the repo's folder, e.g. 'apt-get update && apt-get install -y make && make test'.")],
                   distro: Annotated[Literal["ubuntu", "debian", "fedora", "rocky", "alpine", "arch", "opensuse", "node", "python", "go"],
                                     Field(description="Linux to try it on (node/python/go come with that toolchain on Debian).")] = "ubuntu",
                   image: Annotated[str, Field(description="Any other Docker image instead of a distro.")] = "",
                   offline: Annotated[bool, Field(description="No network at all (nothing can be downloaded).")] = False,
                   timeout_seconds: Annotated[int, Field(ge=10, le=3600, description="How long the command may run before it is stopped.")] = 900,
                   keep: Annotated[bool, Field(description="Keep the sandbox for more commands (sandbox tool); else it is removed.")] = False) -> dict:
    """Try a repo in a throwaway Linux container on Hermes Studio's machine: a clean system, or another distro, with nothing
    of this machine inside. The repo is copied in as pushed (its home remote, current branch). Returns exit code and output."""
    body = {"command": command, "distro": distro, "image": image, "network": "none" if offline else "internet",
            "timeout_seconds": timeout_seconds, "keep": keep}
    return _call("POST", _r(repo) + "/sandbox", body, timeout_seconds + 700)


@server.tool(annotations=WRITE)
def sandbox(action: Annotated[Literal["list", "create", "exec", "write_file", "read_file", "copy_repo", "extend", "remove"], Field(description="What to do.")],
            arguments: Annotated[dict, Field(description='The action\'s fields: create {distro or image, network internet/none, minutes, purpose}; '
                                                         'exec {sandbox, command, workdir, timeoutSeconds}; write_file {sandbox, path, content}; '
                                                         'read_file {sandbox, path}; copy_repo {sandbox, repo: git URL, ref}; extend {sandbox, minutes}; remove {sandbox}.')] = {}) -> dict:
    """Studio's throwaway containers, step by step (try_in_sandbox does create, copy, run and remove in one go). Remove sandboxes you are done with."""
    wait = int((arguments or {}).get("timeoutSeconds") or 300) + 90 if action == "exec" else 700
    return _call("POST", f"/api/sandboxes/{action}", arguments or {}, wait)


# ------------------------------------------------------------------ chats
@server.tool(annotations=READ)
def list_chats(repo: Annotated[str, Field(description='Only this repo\'s chats ("_general" for chats about no repo); empty for all.')] = "") -> list[dict]:
    """The user's chats (of one repo, or all), newest first, with what each is about."""
    out = _call("GET", "/api/chats")
    return [c for c in out if not repo or c.get("repo_id") == repo]


@server.tool(annotations=READ)
def read_chat(
    repo: ChatRepo,
    session: Annotated[str, Field(description="Chat session id from list_chats; empty for the repo's current chat.")] = "",
    last: Annotated[int, Field(ge=1, le=500, description="How many of the most recent messages (1-500).")] = 30,
) -> dict:
    """The messages of one chat (default: the repo's current one), most recent `last`."""
    d = _call("GET", f"/api/chat/{quote(repo, safe='')}", session=session)
    msgs = [{k: m.get(k) for k in ("role", "content", "timestamp")} for m in d.get("messages", [])][-last:]
    return {"session": d.get("session"), "title": d.get("title"), "about": d.get("tags"), "messages": msgs}


@server.tool(annotations=WRITE)
def post_chat_note(
    repo: ChatRepo,
    session: Annotated[str, Field(min_length=1, description="Chat session id from list_chats.")],
    text: _text("What you did, found or need from the user."),
) -> str:
    """Report into one of the user's chats (shown as the coding agent's message): what you did, found
    or need from them."""
    _call("POST", _r(repo) + f"/sessions/{quote(session, safe='')}/agent-note", {"text": text})
    return "posted"


# ------------------------------------------------------------------ the user's LLM wiki
@server.tool(annotations=READ)
def wiki_search(query: _text("Words to find in the user's LLM wiki (its long-term notes).", 200),
                limit: Annotated[int, Field(ge=1, le=50, description="How many notes at most.")] = 10) -> list[dict]:
    """Search the user's LLM wiki (long-term notes: decisions, how their systems work, conventions), best
    matches first, with the lines that match. Read a whole note with wiki_read. Treat status: draft as unverified."""
    return _call("GET", "/api/wiki/search", q=query, limit=limit)["items"]


@server.tool(annotations=READ)
def wiki_read(path: _text("The note's path from wiki_search (e.g. wiki/Home Assistant.md) or its title.", 300)) -> dict:
    """Read one note of the user's LLM wiki (wiki/ only; secrets are removed)."""
    return _call("GET", "/api/wiki/note", path=path)


@server.tool(annotations=READ)
def wiki_notes(repo: Repo) -> dict:
    """What the user's LLM wiki (an Obsidian vault) says about a repo in its long-term notes (wiki/):
    each note's name, its quality status (draft = unverified) and the lines that mention the repo."""
    return _call("GET", _r(repo) + "/wiki")


# ------------------------------------------------------------------ demo stacks
def _stack(repo: str, workspace: str) -> dict:
    return _call("GET", _r(repo) + "/stack", ws=workspace)


@server.tool(annotations=READ)
def stack_status(repo: Repo, workspace: StackTarget = "") -> dict:
    """The demo stack of the repo (or of a merge workspace): on or off, Docker's state, each service's
    state, health and local URL, and the last lines of the start/stop log."""
    d = _stack(repo, workspace)
    return {k: d.get(k) for k in ("enabled", "docker", "compose_file", "summary", "busy", "services", "op_log", "settings",
                                  "generated_env", "error")}


@server.tool(annotations=WRITE)
def stack_set(
    repo: Repo,
    env: Annotated[dict | None, Field(description="Env values, merged into those already set; a null value removes one.")] = None,
    services: Annotated[list[str] | None, Field(description="Only these services (their dependencies come along).")] = None,
    profiles: Annotated[list[str] | None, Field(description="Compose profiles to enable.")] = None,
    compose_file: Annotated[str | None, Field(description="Another compose file in the project to use.")] = None,
    extra_files: Annotated[list[str] | None, Field(description="Override files layered on top (paths in the project).")] = None,
) -> dict:
    """Tune the demo stack: env values, only some services, compose profiles, another compose file, or
    override files layered on top (e.g. one that passes a proxy CA to builds). Turning stacks on is the user's."""
    if env is not None:  # merge into what is set; a null value removes that name
        current = _stack(repo, "").get("settings", {}).get("env", {})
        env = {k: v for k, v in {**current, **env}.items() if v is not None}
    patch = {k: v for k, v in {"env": env, "services": services, "profiles": profiles, "compose_file": compose_file,
                               "extra_files": extra_files}.items() if v is not None}
    return _call("POST", _r(repo) + "/stack/settings", patch)["settings"]


@server.tool(annotations=WRITE)
def stack_up(
    repo: Repo, workspace: StackTarget = "",
    build: Annotated[bool, Field(description="Rebuild its images, after you changed their code.")] = False,
    wait_seconds: Annotated[int, Field(ge=0, le=1200, description="How long to wait for it to be up (0-1200).")] = 240,
) -> dict:
    """Start the isolated demo stack of the repo or of a merge workspace (to test your change). Images
    it builds are built when missing. Waits for it to be up, then returns its status (op_log says why if
    it failed)."""
    import time
    _call("POST", _r(repo) + "/stack/up", {"ws": workspace or None, "build": build})
    deadline = time.time() + wait_seconds
    while True:
        st = stack_status(repo, workspace)
        if not st.get("busy") or time.time() > deadline:
            return st
        time.sleep(3)


@server.tool(annotations=DESTRUCTIVE)
def stack_down(
    repo: Repo, workspace: StackTarget = "",
    reset_data: Annotated[bool, Field(description="Also delete its demo data and generated secrets.")] = False,
) -> dict:
    """Stop the demo stack. reset_data also deletes its demo data and generated secrets."""
    return _call("POST", _r(repo) + "/stack/down", {"ws": workspace or None, "reset": reset_data})


@server.tool(annotations=READ)
def stack_logs(
    repo: Repo,
    service: Annotated[str, Field(description="One service; empty for all.")] = "",
    workspace: StackTarget = "",
    tail: Annotated[int, Field(ge=1, le=5000, description="Lines per service (1-5000).")] = 200,
) -> dict:
    """Recent logs of the demo stack (one service or all), with a count and the last lines of anything
    that looks like an error, per service."""
    d = _call("GET", _r(repo) + "/stack/logs", ws=workspace, service=service, tail=tail)
    return {"errors": d.get("errors"), "text": d.get("text")}


# Demo-stack only, but a request or command can delete demo data, so both carry destructiveHint.
@server.tool(annotations=DESTRUCTIVE)
def stack_request(
    repo: Repo, service: Service,
    path: Annotated[str, Field(pattern=r"^/", description="Path on the service, starting with /.")] = "/",
    method: Annotated[str, Field(pattern=r"(?i)^(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)$",
                                description="HTTP method: GET, POST, PUT, PATCH, DELETE, HEAD or OPTIONS.")] = "GET",
    workspace: StackTarget = "",
    port: Annotated[int, Field(ge=0, le=65535, description="Container port when the service publishes several; 0 for its only one.")] = 0,
    headers: Annotated[dict | None, Field(description="Extra request headers.")] = None,
    body: Annotated[Any, Field(description="Request body; objects and arrays are sent as JSON.")] = None,
) -> dict:
    """Send an HTTP request to a service of the demo stack (seed demo data, call its API, check a page)."""
    return _call("POST", _r(repo) + "/stack/request", {"ws": workspace or None, "service": service, "path": path,
                                                     "method": method.upper(), "port": port or None, "headers": headers,
                                                     "body": body})


@server.tool(annotations=DESTRUCTIVE)
def stack_exec(
    repo: Repo, service: Service,
    command: _text("The command, split like a shell would but run without one; use \"sh -c '...'\" for pipes.", 4000),
    workspace: StackTarget = "",
    timeout: Annotated[int, Field(ge=1, le=1800, description="Seconds before it is stopped (1-1800).")] = 120,
) -> dict:
    """Run a command inside a running service of the demo stack. For CLI tools, migrations, creating
    demo users, reading files."""
    return _call("POST", _r(repo) + "/stack/exec", {"ws": workspace or None, "service": service, "command": command,
                                                  "timeout": timeout})


# ------------------------------------------------------------------ tasks


@server.tool(annotations=READ)
def list_tasks(
    repo: Annotated[str, Field(description="Only this repo (id from list_repos); empty for every repo.")] = "",
    status: Annotated[Literal["", "open", "working", "parked", "done", "dropped"], Field(
        description="Only tasks with this status; empty for all.")] = "",
) -> list[dict]:
    """Tasks the user (or an agent) wrote down: fixes, bugs, changes, ideas, and where each lives now."""
    return _call("GET", "/api/tasks", repo=repo, status=status)["items"]


@server.tool(annotations=WRITE)
def create_task(
    repo: ChatRepo,
    title: _text("Short title, e.g. 'Fix the off-by-one in fetch paging'.", 200),
    body: Annotated[str, Field(max_length=20000, description="What and why, steps to reproduce, ideas.")] = "",
    kind: Annotated[Literal["fix", "bug", "feature", "chore", "merge", "idea"], Field(description="What sort of work.")] = "fix",
    commits: Annotated[list[str], Field(max_length=12, description="Commit ids this is about (fix this commit, etc.).")] = [],
    park: Annotated[Literal["", "wiki", "orb"], Field(
        description='"wiki" parks it in the LLM wiki inbox, "orb" in Pondering Orb; empty keeps it in Upstream Manager.')] = "",
    priority: Annotated[Literal["urgent", "high", "normal", "low"], Field(description="How soon: urgent only for breakage that blocks users.")] = "normal",
) -> dict:
    """Write down a piece of work for later or for the user: it shows on the Work page, linked to its commits.
    Use it when you find a bug or a follow-up that is outside what you were asked to do."""
    about = [{"type": "commit", "id": c, "label": c[:12]} for c in commits]
    return _call("POST", "/api/tasks", {"repo": repo, "title": title, "body": body, "kind": kind, "about": about,
                                         "priority": priority, **({"to": park} if park else {})})


@server.tool(annotations=WRITE)
def update_task(
    task: TaskId,
    status: Annotated[Literal["", "open", "working", "parked", "done", "dropped"], Field(description="New status; empty to keep it.")] = "",
    note: Annotated[str, Field(max_length=2000, description="A line for the task's log: progress, what you found, what is left.")] = "",
) -> dict:
    """Report progress on a task, or mark it done when the change is submitted."""
    return _call("POST", f"/api/tasks/{quote(task, safe='')}", {"status": status or None, "note": note or None})["task"]


@server.tool(annotations=READ)
def herdr_agents() -> list[dict]:
    """The coding agents herdr is running now, on this machine and every machine herdr reaches: name, kind,
    status (idle, working, blocked, done) and the task each is on. Use it to pick who gets a task."""
    return _call("GET", "/api/herdr/agents")["items"]


@server.tool(annotations=WRITE)
def send_task(
    task: TaskId,
    to: Annotated[Literal["herdr", "studio", "hermes", "wiki", "orb"], Field(
        description="herdr: a coding agent in herdr; studio: a Hermes Studio run; hermes: the Hermes gateway; "
                    "wiki / orb: park it in the LLM wiki or Pondering Orb for later.")],
    agent_kind: Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$|^$", description="For herdr: start a new agent of this kind (claude, codex, hermes...); empty for the default.")] = "",
    agent: Annotated[str, Field(pattern=r"^([a-z][a-z0-9_-]{0,31}|w\d+:p\d+)$|^$", description="For herdr: give it to this running agent instead (a name from herdr_agents).")] = "",
    machine: Annotated[str, Field(max_length=100, description="For herdr: one of its saved machines; empty for the usual one.")] = "",
    message: Annotated[str, Field(max_length=20000, description="A follow-up for a task that is already with an agent; empty sends the task itself.")] = "",
) -> dict:
    """Hand a task to an agent (it works on the task's own branch; the user reviews and approves the result),
    or park it. Create the task first with create_task. Sending to agents can be switched off by the user,
    and only a few agent-sent tasks may run at once; an agent that asks something is answered by the user."""
    body = {"to": to, **({"agent_kind": agent_kind} if agent_kind else {}), **({"agent": agent} if agent else {}),
            **({"machine": machine} if machine else {}), **({"message": message} if message else {})}
    out = _call("POST", f"/api/tasks/{quote(task, safe='')}/send", body)
    return {"where": out.get("where"), "status": (out.get("task") or {}).get("status")}


@server.tool(annotations=READ)
def task_progress(task: TaskId) -> dict:
    """How a task that is with an agent is going: the agent's status and recent output (herdr) or conversation
    (Studio, Hermes), plus the task's branch: its commits and changed files. Poll this rather than waiting."""
    tid = quote(task, safe="")
    out = {"task": _call("GET", f"/api/tasks/{tid}")["task"]}
    try:
        a = _call("GET", f"/api/tasks/{tid}/agent")
        out["agent"] = {k: a.get(k) for k in ("type", "agent", "kind", "machine", "status", "pending", "studio_url", "url", "run") if a.get(k) is not None}
        out["agent"]["output"] = (a.get("output") or "")[-4000:] or None
        out["agent"]["messages"] = (a.get("messages") or [])[-6:] or None
    except AppError as e:
        out["agent"] = {"error": str(e)}
    try:
        c = _call("GET", f"/api/tasks/{tid}/changes")
        out["branch"] = {k: c.get(k) for k in ("branch", "onto", "path", "next_step", "ready", "warning", "proposed_message")}
        out["branch"]["commits"] = [{"sha": x["sha"], "subject": x["subject"]} for x in c.get("commits") or []][:30]
        out["branch"]["files"] = c.get("files", [])[:60]
    except AppError as e:
        out["branch"] = {"error": str(e)}
    return out


# ------------------------------------------------------------------ references
RefPath = Annotated[str, Field(description='A path in the reference library from list_references ("" for the top); '
                                           '"file.zip!inner/file.txt" reads a file inside a zip.')]


@server.tool(annotations=READ)
def list_references(path: RefPath = "") -> dict:
    """The user's reference library: repos cloned for reference and uploaded files (PDFs, zips, images...) in folders.
    Returns the folder's items and the library's folder on disk (you may also read it directly)."""
    return _call("GET", "/api/refs", path=path)


@server.tool(annotations=READ)
def read_reference(path: RefPath) -> dict:
    """Read a reference: a file's text, a PDF's text, a zip's file list, or a folder's tree and READMEs."""
    return _call("GET", "/api/refs/read", path=path)


@server.tool(annotations=READ)
def search_references(query: _text("Words to find in file names and in the text of cloned reference repos.", 200)) -> list[dict]:
    """Search the reference library by file name and content."""
    return _call("GET", "/api/refs/search", q=query)["items"]


@server.tool(annotations=WRITE)
def organize_references(action: Annotated[str, Field(description='"folder" makes a folder; "move" moves or renames an item; '
                                                                '"link" puts an item in another folder too without copying it; "note" sets its note.')],
                        path: Annotated[str, Field(description='The item: for "folder" the parent folder ("" for the top).')] = "",
                        to: Annotated[str, Field(description='"move" and "link": the folder it goes into.')] = "",
                        name: Annotated[str, Field(description='"folder": its name. "move" and "link": a new name (optional).')] = "",
                        note: Annotated[str, Field(description='"note": the text.')] = "") -> dict:
    """Sort the user's reference library. Prefer "link" when one reference belongs under several projects:
    there is then one copy to update and store. Deleting is left to the user."""
    if action == "folder":
        return _call("POST", "/api/refs/folder", {"parent": path, "name": name})
    if action == "move":
        return _call("POST", "/api/refs/move", {"path": path, "to": to, "name": name or None})
    if action == "link":
        return _call("POST", "/api/refs/link", {"target": path, "into": to, "name": name or None})
    if action == "note":
        return _call("POST", "/api/refs/note", {"path": path, "note": note})
    raise AppError('action is one of "folder", "move", "link", "note"')


@server.tool(annotations=WRITE)
def clone_reference(url: _text("The git URL (https://github.com/org/repo).", 500),
                    folder: Annotated[str, Field(description="The library folder to put it in.")] = "",
                    name: Annotated[str, Field(description="Its folder name (optional).")] = "") -> dict:
    """Add a repo to the reference library. If it was cloned before, the new place is a link to that copy."""
    return _call("POST", "/api/refs/clone", {"url": url, "folder": folder, "name": name or None})


@server.tool(annotations=WRITE)
def update_references() -> dict:
    """Bring every reference repo up to date with where it came from (one command; runs in the background).
    Repos the user edited are only fetched, never changed. Call again to see how it went."""
    return _call("POST", "/api/refs/update-all")


@server.tool(annotations=READ)
def edited_references() -> list[dict]:
    """Reference repos the user (or an agent) changed: their changed files and own commits. Treat these like the
    repos being worked on in this conversation."""
    return _call("GET", "/api/refs/edited")["items"]


@server.tool(annotations=READ)
def list_integrations() -> dict:
    """What the user's setup is integrated with and whether each part works right now: the built-in connections
    (herdr, Hermes Studio, the Hermes gateway, Pondering Orb, the LLM wiki), the services they added (other
    containers), and every repo marked integrated, partly integrated, to integrate, or not needed. Use it to know
    what to build against and what is still to do."""
    return _call("GET", "/api/services")


@server.tool(annotations=WRITE)
def mark_integration(repo: Repo,
                     state: Annotated[str, Field(description='"integrated", "partial", "planned" (to integrate) or "none" (not needed).')],
                     note: _text("What works, what is missing, or how it connects.", 1000) = "") -> dict:
    """Record how far a repo is integrated, e.g. after finishing or checking an integration."""
    return _call("POST", _r(repo) + "/integration", {"state": state, "note": note})


# ------------------------------------------------------------------ over the network
# `upstream-mcp --http 0.0.0.0:5002` (or UPSTREAM_MCP_PORT for the web app to start it) serves the same
# tools over MCP's streamable HTTP at http://<host>:<port>/mcp, so agents on other machines can use them.
# Every request must carry `Authorization: Bearer <UPSTREAM_MCP_TOKEN>`; without a token it only listens
# on this machine (127.0.0.1).
def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


class _BearerAuth:
    """ASGI wrapper: refuse any HTTP request without the right bearer token."""

    def __init__(self, app, token: str):
        self.app, self.token = app, token.encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and self.token:
            import hmac
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not (got.startswith(b"Bearer ") and hmac.compare_digest(got[7:].strip(), self.token)):
                body = b'{"error":"send Authorization: Bearer <UPSTREAM_MCP_TOKEN>"}'
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"application/json"), (b"www-authenticate", b"Bearer")]})
                await send({"type": "http.response.body", "body": body})
                return
        await self.app(scope, receive, send)


def http_app(host: str = "127.0.0.1", token: str | None = None):
    from mcp.server.transport_security import TransportSecuritySettings
    token = token if token is not None else os.environ.get("UPSTREAM_MCP_TOKEN", "")
    if not token and not _loopback(host):
        raise SystemExit("Serving MCP beyond this machine needs a token: set UPSTREAM_MCP_TOKEN "
                         "(e.g. python -c 'import secrets; print(secrets.token_urlsafe(32))').")
    # With a token, the token is the protection; the Host check would only refuse LAN names and IPs.
    security = TransportSecuritySettings(enable_dns_rebinding_protection=not token)
    app = server.streamable_http_app(host=host, transport_security=security)
    return _BearerAuth(app, token) if token else app


def serve_http(host: str, port: int, token: str | None = None) -> None:
    import uvicorn
    uvicorn.run(http_app(host, token), host=host, port=port, log_level="warning")


def start_http_in_background(app_url: str) -> str | None:
    """Started by the web app when UPSTREAM_MCP_PORT is set. Returns the URL agents use, or None."""
    port = os.environ.get("UPSTREAM_MCP_PORT")
    if not port:
        return None
    global APP
    APP = app_url.rstrip("/")
    host = os.environ.get("UPSTREAM_MCP_HOST", "127.0.0.1")
    import threading
    import uvicorn
    cfg = uvicorn.Config(http_app(host), host=host, port=int(port), log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True, name="upstream-mcp-http").start()
    return f"http://{host}:{port}/mcp"


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="upstream-mcp", description="Upstream Manager's tools for coding agents (MCP).")
    p.add_argument("--http", metavar="[HOST:]PORT", help="serve over the network (streamable HTTP at /mcp) instead of stdio; "
                   "needs UPSTREAM_MCP_TOKEN unless HOST is 127.0.0.1")
    a = p.parse_args()
    if not a.http:
        server.run("stdio")
        return
    host, _, port = a.http.rpartition(":")
    serve_http(host or "127.0.0.1", int(port))


if __name__ == "__main__":
    main()
