"""Upstream Management — Flask web server.

Serves the GUI (repo list, upstream comparisons, per-repo chat, LLM settings,
approval/request page) and a JSON API the frontend calls.

Endpoints:
  GET  /                              -> main app (repo list)
  GET  /repos                         -> JSON list of tracked repos
  GET  /repo/<id>                     -> single repo + upstream comparisons
  POST /add                           -> add repo by path or URL
  POST /repo/<id>/focus               -> set focus branch
  POST /repo/<id>/upstream            -> add an upstream (multi-upstream watching)
  POST /repo/<id>/upstream/<name>/rebase
                                       -> refresh upstream-tracking + recompute
  POST /repo/<id>/upstream/<name>/enable
                                       -> enable/disable an upstream
  DELETE /repo/<id>/upstream/<name>   -> remove an upstream
  POST /run-daily                     -> run merge-plan + approval logic
  GET  /requests                      -> pending approval requests
  POST /request/<id>/decide           -> approve/deny with a comment
  POST /repo/<id>/chat                -> send a chat message (llama-swap)
  GET  /chat/<id>                     -> chat history for a repo
  GET  /settings, POST /settings      -> the app's own LLM connection + memory settings
  GET  /files, GET/POST/PUT/DELETE /files/* -> read/write files in a repo (GUI editing)
"""
from __future__ import annotations

import os
import re
import threading
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, render_template_string, abort

from . import (actions, agent_context, components, config, credentials, daily_check, edits, github, gitops, memory,
               pull_report, registry, stacks, store, upstream_check, workflows, workspaces)
from .store import settings as get_settings, save_settings

flask_app = Flask(__name__)
flask_app.config["JSON_SORT_KEYS"] = False

# background lock so concurrent daily-checks don't race
_run_lock = threading.Lock()

# Extra hosts (host:port) allowed to send changes, e.g. a proxy in front of the app.
_ALLOWED_HOSTS = {h.strip() for h in os.environ.get("UPSTREAM_ALLOWED_HOSTS", "").split(",") if h.strip()}


def _from_agent() -> bool:
    """A request the MCP server made for a coding agent (it says so in a header)."""
    return request.headers.get("X-Upstream-Client") == "agent"


def _user_only(what: str):
    """Things only the person decides: approvals, accounts, settings, letting stacks run."""
    if _from_agent():
        return jsonify({"ok": False, "error": f"{what} is up to the user, in Upstream Manager itself"}), 403
    return None


@flask_app.before_request
def _refuse_cross_site_writes():
    """Refuse state-changing requests that another web page could forge.

    Any site can make your browser send a "simple" cross-origin POST (text/plain
    or a form) to localhost without asking. Requiring a JSON body type forces a
    CORS preflight, which this app never approves, and a foreign Origin header
    is rejected outright. Reads are unaffected.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    if request.mimetype != "application/json":
        return jsonify({"ok": False, "error": "send requests as application/json"}), 415
    origin = request.headers.get("Origin")
    if origin and origin != "null":
        allowed = {request.host, request.headers.get("X-Forwarded-Host", "")} | _ALLOWED_HOSTS
        if urlparse(origin).netloc not in allowed:
            return jsonify({"ok": False, "error": "cross-site request refused"}), 403
    return None


@flask_app.route("/")
@flask_app.route("/chats")
@flask_app.route("/workflows")
@flask_app.route("/repos")
@flask_app.route("/requests")
@flask_app.route("/settings")
@flask_app.route("/repo/<repo_id>")
def index(repo_id: str | None = None):
    # View paths all serve the same shell; app.js renders from location.pathname.
    base = request.headers.get("X-Upstream-Base", "").rstrip("/")
    return render_template_string(TEMPLATE, base_url=base)


@flask_app.route("/api/repos")
def api_repos():
    repos = registry.repo_summary()
    return jsonify(repos)


@flask_app.route("/api/repo/<repo_id>")
def api_repo(repo_id):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    return jsonify(store.snapshot(repo))


@flask_app.route("/api/add", methods=["POST"])
def api_add():
    data = request.get_json(force=True) or {}
    try:
        repo = registry.add_repo(
            path=data.get("path"),
            url=data.get("url"),
            name=data.get("name"),
            focus_branch=data.get("focus_branch"),
            watch_dir=data.get("watch_dir"),
        )
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/repo/<repo_id>/focus", methods=["POST"])
def api_focus(repo_id):
    data = request.get_json(force=True) or {}
    branch = data.get("branch")
    if not branch:
        abort(400)
    try:
        repo = registry.set_focus_branch(repo_id, branch)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not repo:
        abort(404)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/repo/<repo_id>/upstream", methods=["POST"])
def api_add_upstream(repo_id):
    data = request.get_json(force=True) or {}
    name = data.get("name")
    url = data.get("url")
    branch = data.get("branch")
    if not name or not url:
        abort(400)
    try:
        repo = registry.add_url(repo_id, name, url, branch)
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    if not repo:
        abort(404)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>/rebase", methods=["POST"])
@flask_app.route("/api/repo/<repo_id>/fetch", methods=["POST"])
def api_fetch(repo_id, name=None):
    """Fetch the upstream(s) from the network, then return the fresh comparison."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    ups = [u for u in repo["upstreams"] if name in (None, u["name"])]
    plan = upstream_check.build_merge_plan(repo["path"], repo["id"], repo["name"], ups,
                                           focus_branch=repo.get("focus_branch"), fetch=True)
    return jsonify({"ok": True, "plan": plan.to_dict()})


@flask_app.route("/api/repo/<repo_id>/compare")
def api_compare(repo_id):
    """Ahead/behind and merge-conflict check per upstream, from refs already fetched."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    plan = upstream_check.build_merge_plan(repo["path"], repo["id"], repo["name"], repo["upstreams"],
                                           focus_branch=repo.get("focus_branch"))
    return jsonify({"ok": True, "plan": plan.to_dict(), "git": gitops.status_summary(repo["path"])})


@flask_app.route("/api/repo/<repo_id>/components")
def api_components(repo_id):
    """Every submodule component and its drift, from what is already fetched."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    with store.lock:
        repo["components"] = components.sync(repo)
        store.save_repo(repo)
    return jsonify({"ok": True, "items": components.compare_all(repo)})


@flask_app.route("/api/repo/<repo_id>/components/fetch", methods=["POST"])
def api_components_fetch(repo_id):
    """Fetch one component ({"name": ...}) or all of them, then compare."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    name = (request.get_json(silent=True) or {}).get("name")
    with store.lock:
        repo["components"] = components.sync(repo)
        store.save_repo(repo)
    items = [components.compare(repo["path"], c, do_fetch=True) for c in repo["components"]
             if (name in (None, c["name"])) and c.get("enabled", True)]
    return jsonify({"ok": all(i["status"] != "error" for i in items), "items": items})


@flask_app.route("/api/repo/<repo_id>/components/<path:name>", methods=["POST"])
def api_component_settings(repo_id, name):
    """Edit a component's enabled / branch / watch_paths / after_update.

    Submodule names are usually paths (apps/dayglance), hence <path:name>; the
    fixed routes /components/fetch and /components/request still win.
    """
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    patch = request.get_json(force=True) or {}
    with store.lock:
        comp = next((c for c in repo.get("components") or [] if c["name"] == name), None)
        if not comp:
            abort(404)
        components.update_settings(comp, patch)
        store.save_repo(repo)
    return jsonify({"ok": True, "item": components.compare(repo["path"], comp)})


@flask_app.route("/api/repo/<repo_id>/components/request", methods=["POST"])
def api_components_request(repo_id):
    """Raise the one approval request that would bump every component that is behind."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    focus = repo.get("focus_branch") or gitops.current_branch(repo["path"]) or "main"
    names = (request.get_json(silent=True) or {}).get("names")  # omit for "every component that is behind"
    states = [s for s in components.compare_all(repo) if not names or s["name"] in names]
    req = _component_request(repo, focus, states)
    if not req:
        return jsonify({"ok": False, "error": "nothing to update (press Fetch first to check)"})
    return jsonify({"ok": True, "request": store.snapshot(req)})


# ------------------------------------------------------------ accounts
@flask_app.route("/api/credentials")
def api_credentials():
    """Which accounts are set up (never the values themselves)."""
    return jsonify({"ok": True, **credentials.public()})


@flask_app.route("/api/credentials", methods=["POST"])
def api_credentials_save():
    if (refused := _user_only("Changing accounts")):
        return refused
    """Save any of github_token / dockerhub_username / dockerhub_token ("" removes it)."""
    data = request.get_json(force=True) or {}
    where = {k: credentials.set(k, str(v or "").strip()) for k, v in data.items() if k in credentials.FIELDS}
    return jsonify({"ok": True, "stored": where, **credentials.public()})


@flask_app.route("/api/credentials/test", methods=["POST"])
def api_credentials_test():
    which = (request.get_json(silent=True) or {}).get("which")
    try:
        info = github.whoami() if which == "github" else github.dockerhub_check()
    except github.GitHubError as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, **info})


# ------------------------------------------------------------ publish
def _github_remotes(repo: dict) -> list[dict]:
    out = []
    for name, url in gitops.remotes(repo["path"]).items():
        gh = github.parse_remote(url)
        out.append({"name": name, "url": url, "owner": gh[0] if gh else None, "repo": gh[1] if gh else None})
    return out


@flask_app.route("/api/repo/<repo_id>/publish")
def api_publish_info(repo_id):
    """Everything the Publish tab needs: remotes, branch, what would be pushed, Dockerfiles, workflows."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    branch = gitops.current_branch(repo["path"])
    remotes = _github_remotes(repo)
    for r in remotes:
        ref = f"refs/remotes/{r['name']}/{branch}"
        r["ahead"] = gitops.behind_ahead(repo["path"], branch, ref)[1] if gitops.ref_exists(repo["path"], ref) else None
    origin = next((r for r in remotes if r["name"] == "origin" and r["owner"]), next((r for r in remotes if r["owner"]), None))
    return jsonify({"ok": True, "branch": branch, "remotes": remotes, "owner": origin and origin["owner"],
                    "github_repo": origin and origin["repo"], "accounts": credentials.public(),
                    "dockerfiles": workflows.detect(repo), "workflows": workflows.existing(repo)})


@flask_app.route("/api/repo/<repo_id>/push/request", methods=["POST"])
def api_push_request(repo_id):
    """Raise an approval request to push the current branch (and optionally open a PR)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    data = request.get_json(force=True) or {}
    remote = data.get("remote") or "origin"
    branch = gitops.current_branch(repo["path"])
    rem = next((r for r in _github_remotes(repo) if r["name"] == remote), None)
    if not rem:
        return jsonify({"ok": False, "error": f"no remote named {remote}"}), 400
    ref = f"refs/remotes/{remote}/{branch}"
    commits = gitops.list_commits(repo["path"], f"{ref}..{branch}" if gitops.ref_exists(repo["path"], ref) else branch, 0, 20)
    pr = bool(data.get("open_pr")) and bool(rem["owner"])
    base = data.get("base") or "main"
    if pr and base == branch:
        return jsonify({"ok": False, "error": "a pull request needs a different base branch"}), 400
    rec = store.record_request({
        "id": f"{repo['id']}:push:{branch}", "type": "push", "repo_id": repo["id"], "repo_name": repo["name"],
        "focus_branch": branch, "remote": remote, "remote_url": rem["url"], "branch": branch,
        "head": gitops.short_sha(repo["path"], f"refs/heads/{branch}"),
        "open_pr": pr, "base": base, "owner": rem["owner"], "github_repo": rem["repo"],
        "commits": commits, "safe": True, "status": "pending", "created_at": config.now_iso(),
        "comment": None, "decided_at": None, "upstreams": [], "conflicts": [],
    })
    return jsonify({"ok": True, "request": store.snapshot(rec)})


def _do_push(repo: dict, req: dict) -> list[dict]:
    """Run an approved push request: push, then (optionally) open the PR."""
    try:
        res = github.push(repo["path"], req["remote"], req["branch"], req.get("head"))
    except github.GitHubError as e:
        return [{"upstream": req["remote"], "ok": False, "detail": str(e)}]
    out = [{"upstream": req["remote"], "ok": True, "detail": f"{res['detail']} (using {res['auth']})"}]
    if req.get("open_pr"):
        subjects = "\n".join(f"- {c['subject']}" for c in (req.get("commits") or [])[:20])
        try:
            pr = github.open_pr(req["owner"], req["github_repo"], req["branch"], req["base"],
                                (req.get("commits") or [{}])[0].get("subject") or req["branch"],
                                f"Commits:\n{subjects}\n\nOpened from Upstream Manager.")
            out.append({"upstream": "pull request", "ok": True, "url": pr["url"],
                        "detail": f"{'existing' if pr['existing'] else 'opened'} PR #{pr['number']}: {pr['url']}"})
        except github.GitHubError as e:
            out.append({"upstream": "pull request", "ok": False, "detail": str(e)})
    return out


@flask_app.route("/api/repo/<repo_id>/docker/workflow", methods=["POST"])
def api_docker_workflow(repo_id):
    """Preview ({"commit": false}) or commit the Docker image workflow for a spec."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    data = request.get_json(force=True) or {}
    spec = data.get("spec") or {}
    errors = workflows.validate(spec)
    if errors:
        return jsonify({"ok": False, "errors": errors})
    if not data.get("commit"):
        return jsonify({"ok": True, "yaml": workflows.render(spec), "file": f".github/workflows/{spec['filename']}"})
    focus = repo.get("focus_branch") or gitops.current_branch(repo["path"])
    with _run_lock:
        result = workflows.commit(repo, spec, data.get("remove") or [], focus)
    return jsonify(result)


@flask_app.route("/api/repo/<repo_id>/github/secrets", methods=["POST"])
def api_github_secrets(repo_id):
    """Copy the saved Docker Hub login into the GitHub repo's Actions secrets."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    rem = next((r for r in _github_remotes(repo) if r["name"] == ((request.get_json(silent=True) or {}).get("remote") or "origin")), None)
    if not rem or not rem["owner"]:
        return jsonify({"ok": False, "error": "this repo has no github.com remote"})
    values = {"DOCKERHUB_USERNAME": credentials.get("dockerhub_username"), "DOCKERHUB_TOKEN": credentials.get("dockerhub_token")}
    if not all(values.values()):
        return jsonify({"ok": False, "error": "save your Docker Hub username and access token first (Settings -> Accounts)"})
    try:
        for name, value in values.items():
            github.set_secret(rem["owner"], rem["repo"], name, value)
        names = github.secret_names(rem["owner"], rem["repo"])
    except github.GitHubError as e:
        return jsonify({"ok": False, "error": str(e)})
    return jsonify({"ok": True, "repo": f"{rem['owner']}/{rem['repo']}", "secrets": names})


# ------------------------------------------------------------ github actions
@flask_app.route("/api/repo/<repo_id>/actions")
def api_actions(repo_id):
    """Workflows of a GitHub remote with run stats, triggers and fork suggestions."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    if request.args.get("refresh"):
        actions.clear_cache()
    try:
        data = actions.overview(repo, request.args.get("remote") or "origin")
    except github.GitHubError as e:
        return jsonify({"ok": False, "error": str(e)})
    if data.get("ok") and not repo.get("known_workflows"):
        # First look: today's set is the baseline; later additions get flagged as new.
        with store.lock:
            repo["known_workflows"] = actions.local_files(repo["path"])
            store.save_repo(repo)
    return jsonify(data)


@flask_app.route("/api/workflows")
def api_workflows():
    """Workflow health of every watched repo with a GitHub origin, fetched in parallel."""
    from concurrent.futures import ThreadPoolExecutor
    if request.args.get("refresh"):
        actions.clear_cache()

    def one(repo):
        base = {"repo_id": repo["id"], "repo_name": repo.get("name") or repo["id"]}
        try:
            return {**base, **actions.health(repo)}
        except Exception as e:  # GitHub errors, rate limits, network
            return {**base, "ok": False, "error": str(e)}
    repos = store.snapshot(store.all_repos())
    with ThreadPoolExecutor(max_workers=6) as pool:
        return jsonify(list(pool.map(one, repos)))


@flask_app.route("/api/repo/<repo_id>/actions/reviewed", methods=["POST"])
def api_actions_reviewed(repo_id):
    """Mark the current workflow files as reviewed (clears the "new" flags)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    with store.lock:
        repo["known_workflows"] = actions.local_files(repo["path"])
        store.save_repo(repo)
    return jsonify({"ok": True, "count": len(repo["known_workflows"])})


@flask_app.route("/api/repo/<repo_id>/actions/request", methods=["POST"])
def api_actions_request(repo_id):
    """Raise an approval request to disable and/or enable workflows on GitHub."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    data = request.get_json(force=True) or {}
    try:
        info = actions.overview(repo, data.get("remote") or "origin")
    except github.GitHubError as e:
        return jsonify({"ok": False, "error": str(e)})
    if not info.get("ok"):
        return jsonify(info)
    by_id = {w["id"]: w for w in info["workflows"]}
    pick = lambda ids, want: [{"id": w["id"], "name": w["name"], "path": w["path"], "reasons": w["reasons"],
                               "stats": w["stats"]} for i in ids if (w := by_id.get(int(i))) and w["state"] in want]
    disable = pick(data.get("disable") or [], {"active"})
    enable = pick(data.get("enable") or [], {"disabled_manually", "disabled_inactivity"})
    if not disable and not enable:
        return jsonify({"ok": False, "error": "nothing to change (pick active workflows to disable or disabled ones to enable)"})
    rec = store.record_request({
        "id": f"{repo['id']}:actions", "type": "actions", "repo_id": repo["id"], "repo_name": repo["name"],
        "focus_branch": repo.get("focus_branch"), "owner": info["owner"], "github_repo": info["repo"],
        "fork": info["fork"], "parent": info["parent"], "disable": disable, "enable": enable,
        "safe": True, "status": "pending", "created_at": config.now_iso(), "comment": None, "decided_at": None,
        "upstreams": [], "conflicts": [],
    })
    return jsonify({"ok": True, "request": store.snapshot(rec)})


def _do_actions(req: dict) -> list[dict]:
    """Run an approved actions request: one API call per workflow."""
    out = []
    for enable, items in ((False, req.get("disable") or []), (True, req.get("enable") or [])):
        for w in items:
            try:
                actions.set_state(req["owner"], req["github_repo"], w["id"], enable)
                out.append({"upstream": w["name"], "ok": True, "detail": "enabled" if enable else "disabled"})
            except github.GitHubError as e:
                out.append({"upstream": w["name"], "ok": False, "detail": str(e)})
                if "no GitHub token" in str(e) or "401" in str(e) or "403" in str(e):
                    break  # the rest would fail the same way
    return out


# ------------------------------------------------------------ pull reports
@flask_app.route("/api/repo/<repo_id>/pull-report")
def api_pull_report(repo_id):
    """How many incoming commits are a no-issue pull vs touch conflicting files,
    for ?upstream=<remote> (into the focus branch) or ?component=<name> (into its pin)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    focus = repo.get("focus_branch") or "HEAD"
    comp_name = request.args.get("component")
    if comp_name:
        comp = next((c for c in repo.get("components") or [] if c["name"] == comp_name), None)
        if not comp:
            abort(404)
        st = components.compare(repo["path"], comp)
        if st["status"] in ("not_checked_out", "not_fetched", "error"):
            return jsonify({"ok": False, "error": st["detail"]})
        sub = str(Path(repo["path"]) / comp["path"])
        report = pull_report.analyze(sub, st["pin"], f"refs/remotes/origin/{st['branch']}", comp.get("watch_paths"))
        return jsonify({**report, "source": f"component {comp_name}", "into": f"pin {st['pin'][:10]}"})
    up = next((u for u in repo.get("upstreams", []) if u["name"] == request.args.get("upstream")), None)
    if not up:
        abort(404)
    ref = f"refs/remotes/{up['name']}/{up.get('branch') or focus}"
    if not gitops.ref_exists(repo["path"], ref):
        return jsonify({"ok": False, "error": f"{up['name']}/{up.get('branch') or focus} not fetched yet - press Fetch"})
    report = pull_report.analyze(repo["path"], focus, ref)
    return jsonify({**report, "source": f"{up['name']}/{up.get('branch') or focus}", "into": focus})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>/request-pull", methods=["POST"])
def api_request_pull(repo_id, name):
    """Raise an approval request to merge the upstream up to `target` (the clean part)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    up = next((u for u in repo.get("upstreams", []) if u["name"] == name), None)
    target = (request.get_json(silent=True) or {}).get("target")
    if not up or not target or not gitops.ref_exists(repo["path"], target):
        abort(400)
    focus = repo.get("focus_branch") or gitops.current_branch(repo["path"]) or "main"
    behind, ahead = gitops.behind_ahead(repo["path"], focus, target)
    rec = store.record_request({
        "id": f"{repo['id']}:{focus}", "repo_id": repo["id"], "repo_name": repo["name"], "focus_branch": focus,
        "conflicts": [], "safe": True, "status": "pending", "created_at": config.now_iso(),
        "comment": None, "decided_at": None,
        "upstreams": [{"name": up["name"], "url": up.get("url"), "branch": up.get("branch") or focus,
                       "target": gitops.short_sha(repo["path"], target), "behind": behind, "ahead": ahead,
                       "conflict": False, "conflict_files": [],
                       "reason": f"pull the clean part: {behind} commit(s) up to {target[:10]} (merges without conflicts)"}],
    })
    return jsonify({"ok": True, "request": store.snapshot(rec)})


# ------------------------------------------------------- demo stacks (stacks.py)
def _stack_call(repo_id: str, fn):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        return jsonify({"ok": True, **fn(repo)})
    except (stacks.StackError, workspaces.WorkspaceError) as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@flask_app.route("/api/repo/<repo_id>/stack")
def api_stack(repo_id):
    """A demo stack's settings, Docker's state and what runs (?ws= for a merge workspace's stack)."""
    def fn(repo):
        ok, docker = stacks.docker_ok()
        return {"settings": stacks.settings(repo), "docker": {"ok": ok, "detail": docker},
                "generated_env": sorted(repo.get("stack_generated") or {}),
                **stacks.status(repo, request.args.get("ws") or None)}
    return _stack_call(repo_id, fn)


@flask_app.route("/api/repo/<repo_id>/stack/settings", methods=["POST"])
def api_stack_settings(repo_id):
    """{enabled, compose_file, profiles, env, services}. Turning stacks on or off is the user's call."""
    data = request.get_json(force=True) or {}
    if "enabled" in data and (refused := _user_only("Turning demo stacks on or off")):
        return refused
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    s = stacks.settings(repo)
    if "enabled" in data:
        s["enabled"] = bool(data["enabled"])
    if "compose_file" in data:
        s["compose_file"] = str(data["compose_file"] or "").strip()
    for key in ("profiles", "services"):
        if key in data:
            if not (isinstance(data[key], list) and all(isinstance(x, str) for x in data[key])):
                return jsonify({"ok": False, "error": f"{key} must be a list of names"}), 400
            s[key] = data[key]
    if "env" in data:
        if not isinstance(data["env"], dict):
            return jsonify({"ok": False, "error": "env must be an object"}), 400
        s["env"] = {str(k): str(v) for k, v in data["env"].items() if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", str(k))}
    with store.lock:
        repo["stack"] = s
        store.save_repo(repo)
    return jsonify({"ok": True, "settings": s})


@flask_app.route("/api/repo/<repo_id>/stack/up", methods=["POST"])
def api_stack_up(repo_id):
    data = request.get_json(silent=True) or {}
    return _stack_call(repo_id, lambda repo: stacks.up(repo, data.get("ws") or None, build=data.get("build", True) is not False))


@flask_app.route("/api/repo/<repo_id>/stack/down", methods=["POST"])
def api_stack_down(repo_id):
    data = request.get_json(silent=True) or {}
    return _stack_call(repo_id, lambda repo: stacks.down(repo, data.get("ws") or None, bool(data.get("reset"))))


@flask_app.route("/api/repo/<repo_id>/stack/logs")
def api_stack_logs(repo_id):
    a = request.args
    return _stack_call(repo_id, lambda repo: stacks.logs(repo, a.get("ws") or None, a.get("service") or None, int(a.get("tail", 200))))


@flask_app.route("/api/repo/<repo_id>/stack/request", methods=["POST"])
def api_stack_request(repo_id):
    """Send an HTTP request to one of the stack's services: {service, path, method, port?, headers?, body?, ws?}."""
    d = request.get_json(force=True) or {}
    return _stack_call(repo_id, lambda repo: stacks.request(repo, d.get("ws") or None, d.get("service", ""), d.get("path") or "/",
                                                          d.get("method") or "GET", d.get("port"), d.get("headers"), d.get("body")))


@flask_app.route("/api/repo/<repo_id>/stack/exec", methods=["POST"])
def api_stack_exec(repo_id):
    """Run a command in a running service: {service, command (string or list), timeout?, ws?}."""
    d = request.get_json(force=True) or {}

    def fn(repo):
        stacks._check_enabled(repo)
        return stacks.exec_(repo, d.get("ws") or None, d.get("service", ""), d.get("command"), int(d.get("timeout", 120)))
    return _stack_call(repo_id, fn)


# ------------------------------------------------- coding agents (see mcp_server.py)
@flask_app.route("/api/agent-setup")
def api_agent_setup():
    """What a coding agent's MCP config needs: the command that starts the server, and this app's URL."""
    root = str(config.PROJECT_ROOT)
    url = request.host_url.rstrip("/")  # the app itself, not a proxy prefix: the MCP server calls /api directly
    return jsonify({"command": ["uv", "run", "--project", root, "upstream-mcp"], "app_url": url, "project": root})



@flask_app.route("/api/repo/<repo_id>/briefing")
def api_briefing(repo_id):
    """The repo's situation as Markdown, for a coding agent."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    return jsonify({"ok": True, "text": agent_context.briefing(repo)})


@flask_app.route("/api/repo/<repo_id>/conflicts")
def api_conflicts(repo_id):
    """For ?upstream=<remote>: each conflicting file with both sides' changes since the merge base."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        return jsonify(agent_context.conflicts(repo, request.args.get("upstream", "")))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@flask_app.route("/api/repo/<repo_id>/workspaces")
def api_workspaces(repo_id):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    return jsonify({"ok": True, "items": workspaces.list_workspaces(repo)})


@flask_app.route("/api/repo/<repo_id>/workspaces", methods=["POST"])
def api_open_workspace(repo_id):
    """Start merging {"upstream": name}, or a set {"branches": [branch, ...]} (local branches,
    remote branches like origin/feature, or upstream names), in a worktree of its own."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    data = request.get_json(silent=True) or {}
    branches = data.get("branches")
    if branches is not None and not (isinstance(branches, list) and all(isinstance(b, str) for b in branches)):
        return jsonify({"ok": False, "error": "branches must be a list of names"}), 400
    try:
        with _run_lock:
            ws = workspaces.open_workspace(repo, data.get("upstream") or None, branches,
                                           (str(data.get("change") or "").strip()[:120] or None))
    except workspaces.WorkspaceError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True, "workspace": ws})


@flask_app.route("/api/repo/<repo_id>/workspaces/<name>")
def api_workspace(repo_id, name):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        return jsonify({"ok": True, "workspace": workspaces.status(repo, name)})
    except workspaces.WorkspaceError as e:
        return jsonify({"ok": False, "error": str(e)}), 404


@flask_app.route("/api/repo/<repo_id>/workspaces/<name>/continue", methods=["POST"])
def api_continue_workspace(repo_id, name):
    """Commit the resolved merge in progress and merge the rest of the set."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        with _run_lock:
            ws = workspaces.continue_workspace(repo, name)
    except workspaces.WorkspaceError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True, "workspace": ws})


@flask_app.route("/api/repo/<repo_id>/workspaces/<name>/submit", methods=["POST"])
def api_submit_workspace(repo_id, name):
    """Raise an approval request to bring a finished workspace branch into the focus branch."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        ws = workspaces.status(repo, name)
    except workspaces.WorkspaceError as e:
        return jsonify({"ok": False, "error": str(e)}), 404
    if not ws["ready"]:
        return jsonify({"ok": False, "error": f"not ready: {ws['next_step']}", "workspace": ws}), 409
    summary = ((request.get_json(silent=True) or {}).get("summary") or "").strip()[:2000]
    focus = ws["focus_branch"]
    rec = store.record_request({
        "id": f"{repo['id']}:{focus}:{name}", "repo_id": repo["id"], "repo_name": repo["name"], "focus_branch": focus,
        "workspace": name, "summary": summary or None, "sources": ws["sources"],
        "conflicts": [], "safe": True, "status": "pending", "created_at": config.now_iso(),
        "comment": None, "decided_at": None,
        "upstreams": [{"name": "workspace", "url": ws["path"], "branch": ws["branch"], "target": ws["head"],
                       "upstream_sha": ws["head"], "behind": ws["commits_beyond_focus"], "ahead": ws["focus_commits_missing"],
                       "conflict": False, "conflict_files": [],
                       "reason": (f"change: {ws['change']}" if ws.get("change") and not ws["sources"] else
                                  f"merges {', '.join(s['label'] for s in ws['sources']) or 'the workspace'}")
                                 + f" (in workspace {name})" + (f": {summary}" if summary else "")}],
    })
    return jsonify({"ok": True, "request": store.snapshot(rec)})


@flask_app.route("/api/repo/<repo_id>/workspaces/<name>", methods=["DELETE"])
def api_remove_workspace(repo_id, name):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        workspaces.remove(repo, name)
    except workspaces.WorkspaceError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


# ----------------------------------------------------------- model edits
def _edit_message(repo_id: str):
    data = request.get_json(force=True) or {}
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    msg = store.get_message(repo_id, data.get("session"), int(data.get("index", -1)))
    if not msg or not msg.get("edits"):
        abort(404)
    return repo, msg, data


@flask_app.route("/api/repo/<repo_id>/edits/apply", methods=["POST"])
def api_edits_apply(repo_id):
    """Apply the edits the model proposed in one reply ({"session", "index", "only": [i...]})."""
    repo, msg, data = _edit_message(repo_id)
    if msg.get("applied"):
        return jsonify({"ok": False, "error": "already applied; undo first"}), 409
    # Re-read the edits from the reply itself, so a fixed parser or a changed file is always respected.
    proposed = edits.parse(msg["content"])
    chosen = [e for i, e in enumerate(proposed) if data.get("only") is None or i in data["only"]]
    result = edits.apply(repo["path"], chosen)
    with store.lock:
        msg["edit_results"] = result["results"]
        if result["change"]:
            msg["applied"] = result["change"]
        store.save_state()
    return jsonify({"ok": result["ok"], "results": result["results"], "applied": bool(result["change"])})


@flask_app.route("/api/repo/<repo_id>/edits/undo", methods=["POST"])
def api_edits_undo(repo_id):
    repo, msg, _ = _edit_message(repo_id)
    if not msg.get("applied"):
        return jsonify({"ok": False, "error": "nothing to undo"}), 409
    restored = edits.undo(repo["path"], msg["applied"])
    with store.lock:
        msg.pop("applied", None)
        msg.pop("edit_results", None)
        store.save_state()
    return jsonify({"ok": True, "restored": restored})


@flask_app.route("/api/repo/<repo_id>/commits")
def api_commits(repo_id):
    """Commits, paged. view=all (focus branch) | behind (upstream has, you lack) | ahead (only yours)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    view = request.args.get("view", "all")
    skip = max(0, int(request.args.get("skip", 0)))
    limit = min(200, max(1, int(request.args.get("limit", 50))))
    q = request.args.get("q", "").strip()
    comp_name = request.args.get("component")
    if comp_name:
        # A component's own history, read inside the submodule.
        comp = next((c for c in repo.get("components") or [] if c["name"] == comp_name), None)
        if not comp:
            abort(404)
        st = components.compare(repo["path"], comp)
        if st["status"] in ("not_checked_out", "not_fetched", "error"):
            return jsonify({"ok": False, "error": st["detail"], "items": []})
        sub = str(Path(repo["path"]) / comp["path"])
        ref = f"refs/remotes/origin/{st['branch']}"
        rev = {"behind": f"{st['pin']}..{ref}", "ahead": f"{ref}..{st['pin']}"}.get(view, st["pin"])
        items = gitops.list_commits(sub, rev, skip, limit, q)
        return jsonify({"ok": True, "items": items, "more": len(items) == limit})
    focus = repo.get("focus_branch") or "HEAD"
    ups = {u["name"]: u for u in repo.get("upstreams", [])}
    up = ups.get(request.args.get("upstream", "")) or next(iter(ups.values()), None)
    upref = f"refs/remotes/{up['name']}/{up.get('branch') or focus}" if up else None
    if view in ("behind", "ahead") and not (upref and gitops.ref_exists(repo["path"], upref)):
        return jsonify({"ok": False, "error": "upstream not fetched yet - press Fetch", "items": []})
    rev = {"all": focus, "behind": f"{focus}..{upref}", "ahead": f"{upref}..{focus}"}.get(view, focus)
    items = gitops.list_commits(repo["path"], rev, skip, limit, q)
    return jsonify({"ok": True, "items": items, "more": len(items) == limit})


@flask_app.route("/api/repo/<repo_id>/commit/<sha>")
def api_commit(repo_id, sha):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    path = repo["path"]
    comp_name = request.args.get("component")
    if comp_name:
        comp = next((c for c in repo.get("components") or [] if c["name"] == comp_name), None)
        if not comp:
            abort(404)
        path = str(Path(repo["path"]) / comp["path"])
    try:
        return jsonify({"ok": True, **gitops.commit_detail(path, sha)})
    except gitops.GitError as e:
        return jsonify({"ok": False, "error": str(e)}), 404


def _in_workspace(repo: dict, name: str | None) -> dict:
    """The repo, or a merge workspace of it (?ws=name) as if it were the repo, for the editor."""
    if not name:
        return repo
    try:
        return {**repo, "path": str(workspaces.path_of(repo, name))}
    except workspaces.WorkspaceError:
        abort(404)


def _repo_file(repo: dict, rel: str) -> Path | None:
    """Resolve `rel` inside the repo, or None if it would escape it."""
    root = Path(repo["path"]).resolve()
    full = (root / rel).resolve()
    return full if full == root or root in full.parents else None


@flask_app.route("/api/repo/<repo_id>/tree")
def api_tree(repo_id):
    """One directory level of the working tree (read-only file browser)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, request.args.get("ws"))
    d = _repo_file(repo, request.args.get("path", ""))
    if d is None or not d.is_dir():
        abort(400)
    root = Path(repo["path"]).resolve()
    entries = []
    for c in d.iterdir():
        if c.name == ".git":
            continue
        entries.append({"name": c.name, "type": "dir" if c.is_dir() else "file",
                        "path": c.relative_to(root).as_posix(), "size": c.stat().st_size if c.is_file() else None})
    entries.sort(key=lambda e: (e["type"] != "dir", e["name"].lower()))
    return jsonify({"ok": True, "path": d.relative_to(root).as_posix() if d != root else "", "entries": entries})


@flask_app.route("/api/repo/<repo_id>/file")
def api_file(repo_id):
    """Text contents of one file (read-only, capped at 1 MB)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, request.args.get("ws"))
    f = _repo_file(repo, request.args.get("path", ""))
    if f is None or not f.is_file():
        abort(404)
    if f.stat().st_size > 1_000_000:
        return jsonify({"ok": False, "error": "file is larger than 1 MB"})
    try:
        text = f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "binary file"})
    return jsonify({"ok": True, "path": request.args.get("path", ""), "content": text, "lines": text.count(chr(10)) + 1,
                    "mtime": str(f.stat().st_mtime_ns)})  # a string: nanoseconds lose precision as a JS number


def _writable(repo: dict, rel: str) -> Path | None:
    """A path inside the repo that the editor may change: not the root, nothing in .git."""
    root = Path(repo["path"]).resolve()
    p = _repo_file(repo, rel or "")
    if p is None or p == root:
        return None
    if ".git" in {x.casefold() for x in p.relative_to(root).parts}:
        return None
    return p


def _rel(repo: dict, p: Path) -> str:
    return p.relative_to(Path(repo["path"]).resolve()).as_posix()


@flask_app.route("/api/repo/<repo_id>/file", methods=["POST"])
def api_file_save(repo_id):
    """Save a file from the editor: {path, content, mtime?}. With mtime (from when it was
    opened), a file changed on disk since then is not overwritten unless {"force": true}."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, (request.get_json(silent=True) or {}).get("ws"))
    data = request.get_json(force=True) or {}
    p = _writable(repo, data.get("path"))
    if p is None or p.is_dir() or not isinstance(data.get("content"), str):
        return jsonify({"ok": False, "error": "can't write that path"}), 400
    if p.exists() and data.get("mtime") and not data.get("force") and str(p.stat().st_mtime_ns) != str(data["mtime"]):
        return jsonify({"ok": False, "error": "the file changed on disk since you opened it", "changed": True}), 409
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data["content"], encoding="utf-8")
    return jsonify({"ok": True, "path": _rel(repo, p), "mtime": str(p.stat().st_mtime_ns)})


@flask_app.route("/api/repo/<repo_id>/changes")
def api_changes(repo_id):
    """Uncommitted changes, for marking files in the editor: [{path, status}] with status
    M (modified), A (added), D (deleted), R (renamed), U (conflicted) or ? (untracked)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, request.args.get("ws"))
    res = gitops._run(["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"], cwd=repo["path"])
    out, parts = [], res.stdout.split("\0") if res.returncode == 0 else []
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        x, y, path = entry[0], entry[1], entry[3:]
        if x == "R" or x == "C":
            i += 1  # the next field is the old name
        code = "U" if "U" in (x, y) or (x, y) in (("A", "A"), ("D", "D")) else "?" if x == "?" else (x if x != " " else y)
        out.append({"path": path, "status": code})
    return jsonify({"ok": True, "items": out})


@flask_app.route("/api/repo/<repo_id>/files/new", methods=["POST"])
def api_file_new(repo_id):
    """Create an empty file, or a folder with {"dir": true}."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, (request.get_json(silent=True) or {}).get("ws"))
    data = request.get_json(force=True) or {}
    p = _writable(repo, data.get("path"))
    if p is None:
        return jsonify({"ok": False, "error": "can't create that path"}), 400
    if p.exists():
        return jsonify({"ok": False, "error": f"{_rel(repo, p)} already exists"}), 409
    if data.get("dir"):
        p.mkdir(parents=True)
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("", encoding="utf-8")
    return jsonify({"ok": True, "path": _rel(repo, p)})


@flask_app.route("/api/repo/<repo_id>/files/rename", methods=["POST"])
def api_file_rename(repo_id):
    """Rename or move {path, to} inside the repo; never over an existing path."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, (request.get_json(silent=True) or {}).get("ws"))
    data = request.get_json(force=True) or {}
    src, dst = _writable(repo, data.get("path")), _writable(repo, data.get("to"))
    if src is None or dst is None or not src.exists():
        return jsonify({"ok": False, "error": "can't rename that path"}), 400
    if dst.exists():
        return jsonify({"ok": False, "error": f"{_rel(repo, dst)} already exists"}), 409
    if src in dst.parents:
        return jsonify({"ok": False, "error": "can't move a folder into itself"}), 400
    dst.parent.mkdir(parents=True, exist_ok=True)
    src.rename(dst)
    return jsonify({"ok": True, "path": _rel(repo, dst)})


@flask_app.route("/api/repo/<repo_id>/files/delete", methods=["POST"])
def api_file_delete(repo_id):
    """Delete a file, or a folder with everything in it. Tracked files can be restored with git."""
    import shutil
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    repo = _in_workspace(repo, (request.get_json(silent=True) or {}).get("ws"))
    p = _writable(repo, (request.get_json(force=True) or {}).get("path"))
    if p is None or not (p.exists() or p.is_symlink()):
        return jsonify({"ok": False, "error": "can't delete that path"}), 400
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()
    return jsonify({"ok": True})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>/enable", methods=["POST"])
def api_enable_upstream(repo_id, name):
    data = request.get_json(force=True) or {}
    enabled = data.get("enabled", True)
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    with store.lock:
        for up in repo.get("upstreams", []):
            if up.get("name") == name:
                up["enabled"] = bool(enabled)
        store.save_repo(repo)
    return jsonify({"ok": True, "repo": store.snapshot(repo)})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>", methods=["DELETE"])
def api_remove_upstream(repo_id, name):
    repo = registry.remove_upstream(repo_id, name)
    if not repo:
        abort(404)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/run-daily", methods=["POST"])
def api_run_daily():
    """Fetch every watched repo and bring it up to date where that is risk-free.

    Fast-forwards happen automatically (clean tree, focus branch checked out, no
    commits of yours to merge). Anything else (both sides moved, conflicts, a
    dirty tree) raises one approval request per repo and branch. Nothing is pushed.
    """
    results = []
    with _run_lock:
        for repo in registry.get_watching_repos():
            focus = repo.get("focus_branch") or gitops.current_branch(repo["path"]) or "main"
            try:
                plan = upstream_check.build_merge_plan(repo["path"], repo["id"], repo["name"], repo["upstreams"],
                                                       focus_branch=focus, fetch=True)
                pending = []
                for up in plan.upstreams:
                    if up.missing_ref:  # say so: a silent skip reads as "up to date"
                        results.append({"repo_id": repo["id"], "repo_name": repo["name"], "upstream": up.name,
                                        "status": "skipped", "detail": up.conflict_detail})
                        continue
                    if up.behind == 0:
                        continue
                    if up.ahead == 0 and not up.conflict:
                        r = daily_check.merge_upstream(repo["path"], up.to_dict(), focus)
                        if r["ok"]:
                            results.append({"repo_id": repo["id"], "repo_name": repo["name"], "upstream": up.name,
                                            "status": "merged", "behind": up.behind, "detail": r["detail"]})
                            continue
                        reason = r["detail"]
                    else:
                        reason = (f"{len(up.conflict_files)} conflicting file(s)" if up.conflict
                                  else f"needs a merge commit ({up.ahead} commit(s) of yours)")
                    pending.append({**up.to_dict(), "reason": reason})
                _check_components(repo, focus, results)
                if pending:
                    store.record_request({
                        "id": f"{repo['id']}:{focus}",
                        "repo_id": repo["id"],
                        "repo_name": repo["name"],
                        "focus_branch": focus,
                        "conflicts": [f"{u['name']}/{u['branch']}" for u in pending if u["conflict"]],
                        "upstreams": pending,
                        "safe": not any(u["conflict"] for u in pending),
                        "status": "pending",
                        "created_at": config.now_iso(),
                        "comment": None,
                        "decided_at": None,
                    })
                    results.append({"repo_id": repo["id"], "repo_name": repo["name"], "status": "request_raised",
                                    "conflicts": [u["name"] for u in pending if u["conflict"]],
                                    "reasons": [u["reason"] for u in pending]})
            except Exception as e:
                results.append({"repo_id": repo["id"], "status": "error", "error": str(e)})
    for r in results:
        _remember_daily(r)
    memory.write_due_logs_soon()  # log any finished day that has no log yet
    return jsonify({"ok": True, "results": results})


def _component_request(repo: dict, focus: str, states: list[dict]) -> dict | None:
    """Raise (or refresh) the single "bump pins" request for a hub, if anything is behind."""
    behind = [s for s in states if s.get("enabled", True) and s.get("behind")]
    if not behind:
        return None
    rec = {
        "id": f"{repo['id']}:components",
        "type": "components",
        "repo_id": repo["id"],
        "repo_name": repo["name"],
        "focus_branch": focus,
        "components": [{k: s.get(k) for k in ("name", "path", "url", "branch", "pin", "upstream", "behind", "local",
                                               "missing", "watched_changes", "after_update", "conflict", "conflict_files")}
                       for s in behind],
        "safe": not any(s.get("local") for s in behind),
        "status": "pending",
        "created_at": config.now_iso(),
        "comment": None,
        "decided_at": None,
    }
    return store.record_request(rec)


def _check_components(repo: dict, focus: str, results: list) -> None:
    with store.lock:
        repo["components"] = components.sync(repo)
        store.save_repo(repo)
    if not repo["components"]:
        return
    states = components.compare_all(repo, do_fetch=True)
    req = _component_request(repo, focus, states)
    if req:
        results.append({"repo_id": repo["id"], "repo_name": repo["name"], "status": "request_raised",
                        "conflicts": [], "reasons": [f"{c['name']} is {c['behind']} commit(s) behind" for c in req["components"]]})
    for s in states:
        if s.get("status") == "error":
            results.append({"repo_id": repo["id"], "status": "error", "error": f"{s['name']}: {s['detail']}"})


def _remember_daily(r: dict) -> None:
    status = r.get("status")
    if status == "merged":
        text = f"Daily check brought in {r.get('behind')} commit(s) from upstream {r.get('upstream')} ({r.get('detail')})."
    elif status == "request_raised":
        text = f"Daily check raised an approval request: {'; '.join(r.get('reasons') or []) or 'see request'}."
    elif status == "error":
        text = f"Daily check failed: {r.get('error')}"
    else:
        return
    try:
        memory.remember(r["repo_id"], f"{config.now_iso()[:10]}: {text}", kind="event")
    except Exception:
        pass


@flask_app.route("/api/requests")
def api_requests():
    return jsonify(store.get_requests())


@flask_app.route("/api/request/<req_id>/decide", methods=["POST"])
def api_decide(req_id):
    if (refused := _user_only("Approving or denying a request")):
        return refused
    data = request.get_json(force=True) or {}
    decision = data.get("decision")  # 'approve' | 'deny'
    comment = data.get("comment")
    if decision not in ("approve", "deny"):
        abort(400)
    req = store.get_request(req_id)
    if not req:
        abort(404)
    if req.get("status") != "pending":
        return jsonify({"ok": False, "error": f"request is already {req.get('status')}"}), 409
    outcome, failed = None, False
    if decision == "approve":
        # Approval runs the merge. Conflicts can't be resolved here: the merge is
        # aborted and reported, and the working tree is left untouched.
        repo = registry.get_repo(req["repo_id"])
        if not repo:
            abort(404)
        if req.get("type") == "actions":
            outcome = _do_actions(req)
            failed = not all(o["ok"] for o in outcome)
            result = store.decide_request(req_id, decision, comment, outcome, failed)
            return jsonify({"ok": not failed, "request": store.snapshot(result)})
        if req.get("type") == "push":
            with _run_lock:
                outcome, failed = _do_push(repo, req), False
            failed = not all(o["ok"] for o in outcome)
            result = store.decide_request(req_id, decision, comment, outcome, failed)
            return jsonify({"ok": not failed, "request": store.snapshot(result)})
        if req.get("type") == "components":
            with _run_lock:
                bumped = components.bump(repo, [c["name"] for c in req.get("components", [])], req["focus_branch"])
            outcome = [{"upstream": r["name"], **r} for r in bumped["results"]] or [{"upstream": "components", "ok": False, "detail": bumped["detail"]}]
            if bumped.get("commit"):
                outcome.append({"upstream": "commit", "ok": True, "detail": f"{bumped['commit'][:10]}: {bumped['detail']}"})
            failed = not bumped["ok"]
            result = store.decide_request(req_id, decision, comment, outcome, failed)
            return jsonify({"ok": not failed, "request": store.snapshot(result)})
        # Merge exactly what the request showed: upstream commits pushed after it
        # was raised were never reviewed, so they wait for the next request.
        with _run_lock:
            outcome = [{"upstream": u["name"], **daily_check.merge_upstream(
                           repo["path"], {**u, "target": u.get("target") or u.get("upstream_sha")}, req["focus_branch"],
                           allow_merge_commit=True)}
                       for u in req.get("upstreams", [])]
        failed = not all(o["ok"] for o in outcome)
        if req.get("workspace") and not failed:  # its branch is in the focus branch now
            try:
                workspaces.remove(repo, req["workspace"])
            except Exception:
                pass
    result = store.decide_request(req_id, decision, comment, outcome, failed)
    try:
        memory.remember(result["repo_id"], (
            f"{config.now_iso()[:10]}: Merge request for branch {result.get('focus_branch')} was {result['status']}"
            f" ({'; '.join(o['detail'] for o in result.get('result') or []) or 'no merge run'}; "
            f"conflicts: {', '.join(map(str, result.get('conflicts') or [])) or 'none'})"
            + (f". Comment: {comment}" if comment else ".")), kind="decision")
    except Exception:
        pass
    return jsonify({"ok": not failed, "request": store.snapshot(result)})


@flask_app.route("/api/repo/<repo_id>/chat", methods=["POST"])
def api_chat(repo_id):
    if not store.chat_owner(repo_id):
        abort(404)
    data = request.get_json(force=True) or {}
    message = data.get("message")
    if not message:
        abort(400)
    from . import llm
    reply = llm.chat(repo_id, message, data.get("session"), data.get("attachments") or [])
    return jsonify(reply)


@flask_app.route("/api/chat/<repo_id>")
def api_chat_history(repo_id):
    """Messages of one session (?session=<id>, default: the active one)."""
    if not store.chat_owner(repo_id):
        abort(404)
    session = store.snapshot(store.get_session(repo_id, request.args.get("session")))
    repo = store.chat_owner(repo_id)
    for m in session["messages"] if repo.get("path") else []:
        # Edits not applied yet are checked against the files as they are now.
        if m.get("edits") and not m.get("applied"):
            m["edits"] = [{k: v for k, v in edits.preview(repo["path"], e).items() if k != "_new"}
                          for e in edits.parse(m["content"])]
    return jsonify({"session": session["id"], "title": session.get("title"), "messages": session["messages"],
                    "tags": session.get("tags", []), "repo_id": repo_id, "repo_name": repo.get("name")})


@flask_app.route("/api/repo/<repo_id>/sessions")
def api_sessions(repo_id):
    if not store.chat_owner(repo_id):
        abort(404)
    return jsonify(store.list_sessions(repo_id))


@flask_app.route("/api/repo/<repo_id>/sessions", methods=["POST"])
def api_new_session(repo_id):
    """Start a fresh conversation (or switch with {"session": id}); memory is kept."""
    if not store.chat_owner(repo_id):
        abort(404)
    sid = (request.get_json(silent=True) or {}).get("session")
    session = store.set_active_session(repo_id, sid) if sid else store.new_session(repo_id)
    return jsonify({"ok": True, "session": session["id"]})


@flask_app.route("/api/repo/<repo_id>/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(repo_id, session_id):
    if not store.chat_owner(repo_id):
        abort(404)
    return jsonify({"ok": store.delete_session(repo_id, session_id), "active": store.get_session(repo_id)["id"]})


@flask_app.route("/api/chats")
def api_all_chats():
    """Every chat with messages, of every repo and the general ones, newest first."""
    return jsonify(store.all_sessions())


@flask_app.route("/api/repo/<repo_id>/sessions/<session_id>/agent-note", methods=["POST"])
def api_agent_note(repo_id, session_id):
    """A coding agent reports into a chat: {"text"}. Shown as the agent's message; no model call."""
    if not store.chat_owner(repo_id):
        abort(404)
    text = ((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"ok": False, "error": "empty note"}), 400
    msg = store.add_chat_message(repo_id, session_id, "agent", text[:20000])
    memory.record_message(repo_id, session_id, "agent", text[:20000], msg["timestamp"])
    return jsonify({"ok": True, "message": msg})


@flask_app.route("/api/repo/<repo_id>/sessions/<session_id>/tags", methods=["POST"])
def api_session_tags(repo_id, session_id):
    """Set what a chat is about: {"tags": [{type: commit|pr|request|push, id, label, repo?, url?}]}."""
    if not store.chat_owner(repo_id):
        abort(404)
    tags = (request.get_json(silent=True) or {}).get("tags")
    if not isinstance(tags, list):
        return jsonify({"ok": False, "error": "tags must be a list"}), 400
    return jsonify({"ok": True, "tags": store.set_session_tags(repo_id, session_id, tags)})


# --------------------------------------------------------------------- memory
@flask_app.route("/api/memory/status")
def api_memory_status():
    memory.get()
    return jsonify(memory.status())


@flask_app.route("/api/memory/reload", methods=["POST"])
def api_memory_reload():
    return jsonify(memory.reload())


@flask_app.route("/api/repo/<repo_id>/memory")
def api_memory(repo_id):
    """All memories of a repo, or a search with ?q= (&scope=all for every repo); ?kind= filters the list."""
    if not registry.get_repo(repo_id):
        abort(404)
    q = request.args.get("q", "").strip()
    try:
        if not q:
            items = memory.list_repo(repo_id, kind=request.args.get("kind") or None)
        elif request.args.get("scope") == "all":
            items = memory.search(q, k=20)
        else:
            items = memory.search(q, repo_id=repo_id, k=20)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "items": []})
    return jsonify({"ok": True, "items": items, **memory.status()})


@flask_app.route("/api/repo/<repo_id>/memory", methods=["POST"])
def api_memory_add(repo_id):
    if not registry.get_repo(repo_id):
        abort(404)
    text = ((request.get_json(force=True) or {}).get("text") or "").strip()
    if not text:
        abort(400)
    return jsonify({"ok": True, "items": memory.remember(repo_id, text, kind="note")})


@flask_app.route("/api/repo/<repo_id>/memory/index-commits", methods=["POST"])
def api_memory_index_commits(repo_id):
    """Store recent commits (subject + touched files) as memories, linked to
    their files, so related changes here and in other repos can be found."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    n = int((request.get_json(silent=True) or {}).get("count", 50))
    done = set(repo.get("indexed_commits") or [])
    added = 0
    for c in gitops.recent_commits(repo["path"], n, with_files=True):
        if c["sha"] in done:
            continue
        files = c.get("files") or []
        shown = ", ".join(files[:15]) + (f" (+{len(files) - 15} more)" if len(files) > 15 else "")
        memory.remember(repo_id, f"Commit {c['sha']} on {c['date'][:10]} by {c['author']}: {c['subject']}. Files: {shown or 'none'}",
                        kind="commit", extra={"sha": c["sha"]}, link={c["sha"].lower(), *memory.refs(" ".join(files))})
        done.add(c["sha"])
        added += 1
    with store.lock:
        repo["indexed_commits"] = sorted(done)
        store.save_repo(repo)
    cleaned = memory.cleanup(repo_id) if memory._settings()["auto_cleanup"] else None
    return jsonify({"ok": True, "added": added, "total": len(done), "cleaned": cleaned})


@flask_app.route("/api/repo/<repo_id>/memory/daylog", methods=["POST"])
def api_memory_daylog(repo_id):
    """Have the model write the log of one day ({"day": "YYYY-MM-DD"}, default today), replacing any earlier one."""
    if not registry.get_repo(repo_id):
        abort(404)
    day = (request.get_json(silent=True) or {}).get("day")
    if day:
        try:
            day = date.fromisoformat(day).isoformat()
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "day must be YYYY-MM-DD"}), 400
    return jsonify(memory.write_day_log(repo_id, day))


@flask_app.route("/api/repo/<repo_id>/memory/cleanup", methods=["POST"])
def api_memory_cleanup(repo_id):
    """Clean up: {"dry_run": true} previews (thorough), {"ids": [...]} deletes exactly those."""
    if not registry.get_repo(repo_id):
        abort(404)
    data = request.get_json(silent=True) or {}
    ids = data.get("ids")
    return jsonify({"ok": True, **memory.cleanup(repo_id, dry_run=bool(data.get("dry_run")), thorough=True,
                                                  only_ids=ids if isinstance(ids, list) else None)})


@flask_app.route("/api/memory/<memory_id>", methods=["DELETE"])
def api_memory_delete(memory_id):
    memory.forget(memory_id)
    return jsonify({"ok": True})


KEY_MASK = "••••"


def _saved_key() -> str:
    return (store.load_llm_config().get("api_key") or "")


def _unmask(key: str | None) -> str | None:
    """The page only ever sees a masked key; a masked value means "keep the saved one"."""
    return _saved_key() if key and key.startswith(KEY_MASK) else key


def _public_settings() -> dict:
    """Settings for the page, with the API key masked (it is never sent back in full)."""
    s = store.snapshot(get_settings())
    llm = s.get("llm") or {}
    if llm.get("api_key"):
        llm["api_key"] = KEY_MASK + (llm["api_key"][-4:] if len(llm["api_key"]) > 8 else "")
    return s


@flask_app.route("/api/settings")
def api_settings():
    return jsonify(_public_settings())


@flask_app.route("/api/settings", methods=["POST"])
def api_save_settings():
    if (refused := _user_only("Changing settings")):
        return refused
    data = request.get_json(force=True) or {}
    if isinstance(data.get("llm"), dict) and "api_key" in data["llm"]:
        data["llm"]["api_key"] = _unmask(data["llm"]["api_key"]) or ""
    save_settings(data)
    if "memory" in data:
        memory.reload()
    return jsonify({"ok": True, "settings": _public_settings()})


def _tracked(repo_id: str | None) -> dict:
    repo = registry.get_repo(repo_id) if repo_id else None
    if not repo:
        abort(404)
    return repo


@flask_app.route("/api/files", methods=["GET"])
def api_files():
    """Legacy directory listing; limited to a tracked repo (prefer /api/repo/<id>/tree)."""
    repo = _tracked(request.args.get("repo"))
    d = _repo_file(repo, request.args.get("path", ""))
    if d is None or not d.is_dir():
        abort(400)
    entries = [{"name": c.name, "type": "dir" if c.is_dir() else "file", "modified": c.stat().st_mtime}
               for c in d.iterdir()]
    return jsonify({"path": str(d), "entries": entries})


@flask_app.route("/api/files/contents", methods=["GET"])
def api_file_contents():
    repo = _tracked(request.args.get("repo"))
    p = _repo_file(repo, request.args.get("path", ""))
    if p is None or not p.is_file():
        abort(404)
    try:
        content = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"error": "binary file"}), 400
    return jsonify({"path": str(p), "content": content})


@flask_app.route("/api/files/contents", methods=["POST"])
def api_file_write():
    data = request.get_json(force=True) or {}
    repo = _tracked(data.get("repo"))
    p = _repo_file(repo, data.get("path") or "")
    if p is None or p == Path(repo["path"]).resolve() or ".git" in {x.casefold() for x in p.relative_to(Path(repo["path"]).resolve()).parts}:
        abort(400)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data.get("content", ""), encoding="utf-8")
    return jsonify({"ok": True, "path": str(p)})


@flask_app.route("/api/repo/<repo_id>/branches")
def api_repo_branches(repo_id):
    return _branches(_tracked(repo_id))


@flask_app.route("/api/branches")
def api_branches():
    """Legacy: ?repo=<path>, accepted only for a tracked repo's path."""
    repo = registry.repo_by_path(request.args.get("repo", ""))
    if not repo:
        abort(404)
    return _branches(repo)


def _branches(repo: dict):
    try:
        out = gitops._git(["branch", "--format=%(refname:short)"], repo["path"])
    except gitops.GitError:
        return jsonify({"branches": []})
    return jsonify({"branches": [l.strip() for l in out.splitlines() if l.strip()]})


@flask_app.route("/api/models")
def api_models():
    """Models the server offers; ?base_url=&api_key= probe a server before saving."""
    from . import llm
    base_url, key = request.args.get("base_url"), request.args.get("api_key")
    if key and key.startswith(KEY_MASK):
        # The saved key only ever goes to the saved server: any page can make the
        # browser load this URL, so a masked key aimed elsewhere is dropped.
        saved = (store.load_llm_config().get("base_url") or "").rstrip("/")
        key = _saved_key() if base_url is None or (base_url or "").rstrip("/") == saved else ""
    models, error = llm.list_models(base_url, key)
    return jsonify({"data": [{"id": m} for m in models], "error": error})


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upstream Manager</title>
<meta id="upstream-base" name="upstream-base" content="{{ base_url }}" />
<style>
  :root {
    --bg: #0f1115; --panel: #13161c; --card: #171a21; --raise: #1e2330; --line: #262b36; --line2: #2f3646;
    --text: #e6e8ee; --soft: #b8c0cf; --muted: #8a93a3; --accent: #3b6cff; --accent2: #9fb4ff;
    --good-bg: #12301f; --good: #7fdca6; --warn-bg: #33280f; --warn: #f0c27a; --bad-bg: #3a1a1d; --bad: #ff9aa2;
    --info-bg: #102c33; --info: #8fd6e3; --mono: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  [hidden] { display: none !important; }
  html, body { height: 100%; }
  body { margin: 0; font: 14px/1.45 Inter, system-ui, -apple-system, "Segoe UI", sans-serif; background: var(--bg); color: var(--text); }
  a { color: var(--accent2); } a:hover { color: #c7d4ff; }
  code, pre, .mono { font-family: var(--mono); }
  header.top { position: sticky; top: 0; z-index: 50; display: flex; flex-wrap: wrap; align-items: center; gap: 12px 16px; padding: 0 20px; min-height: 54px; border-bottom: 1px solid var(--line); background: var(--panel); }
  .brand { display: flex; align-items: center; gap: 10px; font-weight: 700; font-size: 15px; color: var(--text); text-decoration: none; }
  nav.main { display: flex; gap: 2px; flex-wrap: wrap; }
  nav.main a { display: flex; align-items: center; gap: 7px; padding: 7px 11px; border-radius: 8px; color: var(--soft); text-decoration: none; }
  nav.main a:hover { background: var(--raise); color: #fff; }
  nav.main a.on { background: var(--raise); color: #fff; font-weight: 600; }
  .count { background: #d68a1e; color: #1a1205; border-radius: 999px; padding: 0 7px; font-size: 12px; font-weight: 700; line-height: 18px; }
  .count:empty { display: none; }
  .spacer { flex: 1; }
  .status-dot { display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; }
  .status-dot i { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .status-dot.ok i { background: #3fb96f; } .status-dot.bad i { background: #e5484d; }
  main { padding: 20px; max-width: 1240px; margin: 0 auto; }
  main.wide { max-width: none; padding: 14px 20px 20px; }
  h1.page { margin: 0; font-size: 21px; } h2 { font-size: 16px; margin: 0; } h3 { font-size: 15px; margin: 0; } h4 { font-size: 14px; }
  .row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .col { display: flex; flex-direction: column; gap: 10px; }
  .card, .repo-card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .repo-card { margin-bottom: 14px; } .repo-card h3 { margin: 0 0 6px; }
  .panel { background: var(--panel); border: 1px solid var(--line); border-radius: 10px; }
  .badge { display: inline-flex; align-items: center; gap: 4px; background: var(--raise); color: var(--soft); border-radius: 6px; padding: 1px 8px; font-size: 12px; margin-right: 4px; white-space: nowrap; border: 0; }
  .badge.up, .badge.good { background: var(--good-bg); color: var(--good); }
  .badge.warn { background: var(--warn-bg); color: var(--warn); }
  .badge.bad { background: var(--bad-bg); color: var(--bad); }
  .badge.info, .badge.kind-daylog { background: var(--info-bg); color: var(--info); }
  .badge.kind-commit { background: #1d2a40; color: #9fc0ff; } .badge.kind-event { background: var(--warn-bg); color: var(--warn); }
  .badge.kind-decision { background: #2b1d40; color: #c9a8ff; } .badge.kind-note { background: var(--good-bg); color: var(--good); }
  .btn { display: inline-flex; align-items: center; gap: 6px; height: 34px; padding: 0 12px; border-radius: 8px; border: 0; background: var(--accent); color: #fff; font: inherit; font-weight: 600; cursor: pointer; white-space: nowrap; }
  .btn:hover { background: #4f7bff; }
  .btn.ghost { background: var(--card); color: var(--text); border: 1px solid var(--line2); font-weight: 500; }
  .btn.ghost:hover { background: var(--raise); }
  .btn.danger { background: #b4373d; } .btn.danger:hover { background: #c9454b; }
  .btn.small { height: 28px; padding: 0 9px; font-size: 13px; }
  .btn.icon { width: 34px; padding: 0; justify-content: center; }
  .btn.link { background: none; color: var(--accent2); padding: 0 4px; font-weight: 500; }
  .btn:disabled { opacity: .55; cursor: progress; }
  input, select, textarea { background: var(--bg); color: var(--text); border: 1px solid var(--line2); border-radius: 8px; padding: 7px 9px; font: inherit; max-width: 100%; }
  select { height: 34px; padding: 0 8px; } input { height: 34px; }
  :focus-visible { outline: 2px solid #7fa0ff; outline-offset: 1px; }
  .muted { color: var(--muted); font-size: 13px; } .small { font-size: 12px; }
  .loading { color: var(--muted); font-size: 13px; padding: 8px; }
  .loading::before { content: ""; display: inline-block; width: 10px; height: 10px; margin-right: 8px; border: 2px solid #4a5468; border-top-color: var(--accent2); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -1px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .upstream { padding: 10px; border: 1px solid var(--line); border-radius: 8px; margin-top: 8px; }
  /* dashboard */
  .tiles { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 12px; }
  .tile { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 12px 14px; }
  .tile b { display: block; font-size: 22px; margin-top: 2px; }
  .tile.warn b { color: var(--warn); } .tile.bad b { color: var(--bad); } .tile.good b { color: var(--good); }
  .notice { border-radius: 10px; padding: 12px 14px; display: flex; flex-direction: column; gap: 8px; }
  .notice.approve { background: #1b1710; border: 1px solid #4a3a1c; } .notice.approve h2 { color: #f0c27a; }
  .notice.attention { background: #1d1416; border: 1px solid #4a2a30; } .notice.attention h2 { color: #ffb3ba; }
  .notice .item { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; padding-top: 8px; border-top: 1px solid rgba(255,255,255,.06); }
  .notice .item:first-of-type { border-top: 0; padding-top: 0; }
  .notice .item > div { flex: 1 1 320px; min-width: 0; }
  .table-box { overflow-x: auto; border: 1px solid var(--line); border-radius: 10px; background: var(--panel); }
  table.list { width: 100%; min-width: 720px; border-collapse: collapse; }
  table.list th { text-align: left; color: var(--muted); font-size: 12px; font-weight: 600; padding: 9px 14px; border-bottom: 1px solid var(--line); }
  table.list td { padding: 10px 14px; border-bottom: 1px solid #1e2330; vertical-align: top; }
  table.list tr:last-child td { border-bottom: 0; }
  table.list tr.click { cursor: pointer; } table.list tr.click:hover td { background: #171b24; }
  /* repo page */
  .repo-head { display: flex; flex-wrap: wrap; align-items: center; gap: 10px 12px; }
  .path { color: var(--muted); font-size: 12px; max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .ups { margin-top: 10px; }
  .up-row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 12px; border-top: 1px solid #1e2330; }
  .up-row:first-child { border-top: 0; }
  .menu { position: relative; } .menu > summary { list-style: none; } .menu > summary::-webkit-details-marker { display: none; }
  .menu-list { position: absolute; right: 0; top: 38px; z-index: 30; min-width: 190px; background: var(--card); border: 1px solid var(--line2); border-radius: 8px; padding: 4px; box-shadow: 0 8px 24px rgba(0,0,0,.45); display: flex; flex-direction: column; }
  .menu-list button { text-align: left; background: none; border: 0; color: var(--text); padding: 8px 10px; border-radius: 6px; font: inherit; cursor: pointer; }
  .menu-list button:hover { background: var(--raise); } .menu-list button.danger { color: var(--bad); }
  .workbench { display: flex; gap: 0; margin-top: 12px; height: var(--bench-h, calc(100vh - 210px)); min-height: 480px; }
  .pane { min-width: 0; display: flex; flex-direction: column; background: var(--panel); border: 1px solid var(--line); border-radius: 10px; overflow: hidden; }
  .pane.tools { flex: 1 1 auto; }
  .pane.chat { flex: 0 0 var(--chat-w, 440px); }
  .splitter { flex: 0 0 10px; cursor: col-resize; display: flex; align-items: center; justify-content: center; touch-action: none; user-select: none; }
  .splitter::after { content: ""; width: 3px; height: 40px; border-radius: 3px; background: var(--line2); }
  .splitter:hover::after, .splitter.drag::after { background: var(--accent); }
  .workbench.chat-wide .pane.tools, .workbench.chat-wide .splitter { display: none; }
  .workbench.chat-wide .pane.chat { flex: 1 1 auto; }
  .workbench.chat-hidden .pane.chat, .workbench.chat-hidden .splitter { display: none; }
  .tabs { display: flex; gap: 2px; padding: 6px; border-bottom: 1px solid var(--line); overflow-x: auto; flex: none; }
  .tab { height: 32px; padding: 0 11px; border-radius: 7px; border: 0; background: transparent; color: var(--soft); font: inherit; cursor: pointer; white-space: nowrap; display: inline-flex; align-items: center; gap: 6px; }
  .tab:hover { background: #1a1f2a; color: #fff; } .tab.active { background: var(--raise); color: #fff; font-weight: 600; }
  .tab .count { background: var(--warn-bg); color: var(--warn); }
  .tab.chat-tab { display: none; }
  .tabpanel { flex: 1; min-height: 0; overflow: auto; padding: 10px 12px; }
  .tabpanel.flush { padding: 0; overflow: hidden; display: flex; flex-direction: column; }
  /* chat */
  .chat-head { display: flex; align-items: center; gap: 6px; padding: 7px 8px; border-bottom: 1px solid var(--line); flex-wrap: wrap; }
  .chat-head select { flex: 1; min-width: 120px; }
  .about { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; padding: 6px 10px; border-bottom: 1px solid var(--line); font-size: 12px; }
  .chat-box { flex: 1; min-height: 0; overflow-y: auto; padding: 12px; display: flex; flex-direction: column; gap: 10px; }
  .msg { padding: 9px 12px; border-radius: 12px; max-width: 86%; line-height: 1.5; }
  .msg.user { background: #2b4fd1; color: #fff; align-self: flex-end; border-bottom-right-radius: 4px; white-space: pre-wrap; overflow-wrap: anywhere; }
  .msg.bot { background: #1c2029; align-self: flex-start; border-bottom-left-radius: 4px; white-space: normal; overflow-wrap: anywhere; }
  .msg.bot p, .msg.bot ul, .msg.bot ol, .msg.bot blockquote { margin: 0 0 8px; }
  .msg.bot > :last-child { margin-bottom: 0; }
  .msg.bot ul, .msg.bot ol { padding-left: 20px; }
  .msg.bot h3, .msg.bot h4, .msg.bot h5, .msg.bot h6 { margin: 6px 0; font-size: 14px; }
  .msg.bot code { background: #11141a; border: 1px solid var(--line); border-radius: 4px; padding: 0 4px; font: 12px var(--mono); }
  .msg.bot pre.md-code { background: var(--bg); border: 1px solid var(--line); border-radius: 6px; padding: 8px; overflow: auto; margin: 0 0 8px; }
  .msg.bot pre.md-code code { background: none; border: 0; padding: 0; white-space: pre; }
  .msg.bot blockquote { border-left: 3px solid #3a4152; padding-left: 8px; color: var(--soft); }
  .msg.err { background: var(--bad-bg); }
  .msg.agent { background: #102c33; border: 1px solid #1f5560; }
  .composer { border-top: 1px solid var(--line); padding: 8px; display: flex; flex-direction: column; gap: 6px; flex: none; }
  .composer textarea { width: 100%; resize: vertical; min-height: 44px; max-height: 40vh; }
  .tray { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; font-size: 12px; }
  .chip { display: inline-flex; align-items: center; gap: 2px; background: #1d2a40; border-radius: 12px; padding: 1px 4px 1px 9px; font-size: 12px; }
  .chip.about-chip { background: var(--info-bg); color: var(--info); }
  .chip-x { background: none; border: none; color: var(--accent2); cursor: pointer; font-size: 14px; padding: 0 4px; }
  .edits { margin-top: 10px; border-top: 1px solid #3a4152; padding-top: 8px; white-space: normal; }
  /* chats page */
  .chats-layout { display: flex; gap: 12px; height: calc(100vh - 110px); min-height: 520px; }
  .chat-list { flex: 0 0 300px; overflow: auto; display: flex; flex-direction: column; }
  .chat-item { display: block; width: 100%; text-align: left; background: none; border: 0; border-bottom: 1px solid #1e2330; color: var(--text); padding: 10px 12px; font: inherit; cursor: pointer; }
  .chat-item:hover { background: #171b24; } .chat-item.on { background: var(--raise); }
  .chat-item .t { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  /* memory, diff, dialogs */
  .mem-list { margin-top: 10px; }
  .mem { display: flex; gap: 8px; align-items: flex-start; padding: 8px; border: 1px solid var(--line); border-radius: 8px; margin-bottom: 6px; font-size: 13px; }
  .mem-del { padding: 2px 8px; }
  .mem .md p:first-child { margin-top: 0; } .mem .md p:last-child { margin-bottom: 0; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 900; display: flex; align-items: center; justify-content: center; padding: 12px; }
  .overlay-panel { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; width: min(1100px, 100%); max-height: 90vh; overflow: auto; }
  .code-pre { margin: 8px 0 0; font: 12px/18px var(--mono); background: var(--bg); border: 1px solid var(--line); border-radius: 6px; overflow: auto; max-height: 55vh; }
  .dl { white-space: pre; padding: 0 8px; }
  .dl.add { background: #12301c; color: #9be3b0; } .dl.del { background: #3a1d1d; color: #f0a0a0; }
  .dl.hunk { color: #7fb0ff; } .dl.file { color: #e0c070; font-weight: 600; }
  .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin: 12px 0; }
  .stat { border: 1px solid var(--line); border-radius: 8px; padding: 10px; background: #11141a; }
  .stat-n { font-size: 22px; font-weight: 600; }
  .stat.good .stat-n { color: #8fd9a8; } .stat.bad .stat-n { color: #f0a0a0; } .stat.warn .stat-n { color: #e0c070; }
  /* editor */
  .ide { display: flex; flex: 1; min-height: 0; }
  .ide-side { flex: 0 0 260px; min-width: 0; display: flex; flex-direction: column; border-right: 1px solid var(--line); background: #11141a; }
  .ide-side-head { display: flex; align-items: center; gap: 4px; padding: 6px 8px; border-bottom: 1px solid var(--line); font-size: 11px; letter-spacing: .06em; text-transform: uppercase; color: var(--muted); }
  .ide-side-head .spacer { flex: 1; }
  .ide-side-head button { background: none; border: 0; color: var(--soft); cursor: pointer; width: 26px; height: 26px; border-radius: 5px; font-size: 15px; }
  .ide-side-head button:hover { background: var(--raise); color: #fff; }
  .tree { flex: 1; overflow: auto; font-size: 13px; padding: 4px 0; }
  .trow { cursor: pointer; padding: 2px 8px; white-space: nowrap; display: flex; align-items: center; gap: 4px; }
  .trow:hover { background: #1b2030; } .trow.sel { background: #25304a; }
  .trow .nm { overflow: hidden; text-overflow: ellipsis; }
  .trow .st { margin-left: auto; font: 11px var(--mono); padding-left: 6px; }
  .trow.ch-M .nm, .trow.ch-R .nm { color: #e2c08d; } .trow.ch-A .nm, .trow.ch-q .nm { color: #73c991; }
  .trow.ch-D .nm { color: #f48771; text-decoration: line-through; } .trow.ch-U .nm { color: #ff9aa2; font-weight: 600; }
  .trow.has-ch > .nm { color: #e2c08d; }
  .ide-main { flex: 1; min-width: 0; display: flex; flex-direction: column; background: #212121; }
  .ide-tabs { display: flex; overflow-x: auto; background: #181a20; border-bottom: 1px solid var(--line); flex: none; }
  .ide-tab { display: flex; align-items: center; gap: 6px; padding: 0 6px 0 12px; height: 34px; border-right: 1px solid var(--line); color: var(--muted); cursor: pointer; white-space: nowrap; font-size: 13px; background: none; border-top: 0; border-left: 0; border-bottom: 0; font-family: inherit; }
  .ide-tab.on { background: #212121; color: #fff; }
  .ide-tab .x { width: 20px; height: 20px; border-radius: 4px; display: inline-flex; align-items: center; justify-content: center; color: var(--muted); }
  .ide-tab .x:hover { background: #3a3f4b; color: #fff; }
  .ide-tab.dirty .x::before { content: "●"; } .ide-tab.dirty .x:hover::before { content: "×"; } .ide-tab:not(.dirty) .x::before { content: "×"; }
  .ide-bar { display: flex; align-items: center; gap: 8px; padding: 4px 10px; font-size: 12px; color: var(--muted); border-top: 1px solid var(--line); background: #181a20; flex: none; flex-wrap: wrap; }
  .ide-editor { flex: 1; min-height: 0; position: relative; }
  .ide-editor .CodeMirror { position: absolute; inset: 0; height: auto; font: 13px/1.55 var(--mono); }
  .ide-empty { padding: 24px; color: var(--muted); }
  .ide-scope { display: flex; gap: 6px; align-items: center; padding: 6px 8px; border-bottom: 1px solid var(--line); font-size: 12px; }
  .ide-scope select { height: 28px; flex: 1; min-width: 0; font-size: 12px; }
  @media (max-width: 1100px) { .tiles { grid-template-columns: repeat(2, minmax(0, 1fr)); } }
  @media (max-width: 900px) {
    .grid { grid-template-columns: 1fr; }
    .workbench { height: auto; min-height: 0; display: block; }
    .workbench .pane.chat, .workbench .splitter { display: none !important; }
    .workbench .pane.tools { height: calc(100vh - 230px); min-height: 480px; }
    .tab.chat-tab { display: inline-flex; }
    .chat-ctl { display: none !important; }
    .chats-layout { flex-direction: column; height: auto; }
    .chat-list { flex: none; max-height: 240px; }
    .chats-layout .pane.chat { height: calc(100vh - 380px); min-height: 420px; }
  }
  @media (max-width: 700px) {
    main, main.wide { padding: 12px; }
    header.top { padding: 6px 12px; }
    .tiles { gap: 8px; } .tile b { font-size: 18px; }
    .ide-side { flex-basis: 40%; }
    .msg { max-width: 94%; }
    .btn { height: 40px; } .btn.small { height: 34px; } .tab { height: 38px; }
    .path { max-width: 100%; }
  }
</style>
<link rel="stylesheet" href="{{ base_url }}/static/vendor/codemirror/codemirror.css">
</head>
<body>
<header class="top">
  <a class="brand" href="{{ base_url }}/repos">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="#9fb4ff" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="6" cy="6" r="2.5"/><circle cx="6" cy="18" r="2.5"/><circle cx="18" cy="9" r="2.5"/><path d="M6 8.5v7"/><path d="M18 11.5c0 3.5-4 3.5-9.5 5"/></svg>
    Upstream Manager
  </a>
  <nav class="main" aria-label="Main">
    <a href="{{ base_url }}/repos" data-view="repos">Repos</a>
    <a href="{{ base_url }}/chats" data-view="chats">Chats</a>
    <a href="{{ base_url }}/workflows" data-view="workflows">Workflows</a>
    <a href="{{ base_url }}/requests" data-view="requests">Requests <span class="count" id="pending-count"></span></a>
    <a href="{{ base_url }}/settings" data-view="settings">Settings</a>
  </nav>
  <div class="spacer"></div>
  <div class="status-dot" id="model-status"><i></i><span>Model</span></div>
</header>
<main id="main">
  <div id="content" aria-live="polite"><div class="loading">Loading…</div></div>
</main>
<script src="{{ base_url }}/static/md.js"></script>
<script src="{{ base_url }}/static/app.js"></script>
</body>
</html>
"""
