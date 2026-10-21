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
  GET  /settings, POST /settings      -> LLM config (mirrors hermes config.yaml)
  GET  /files, GET/POST/PUT/DELETE /files/* -> read/write files in a repo (GUI editing)
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import requests
from flask import Flask, jsonify, request, render_template_string, abort

from . import config, registry, store, upstream_check, change_engine, daily_check, gitops, memory
from .store import settings as get_settings, save_settings

flask_app = Flask(__name__)
flask_app.config["JSON_SORT_KEYS"] = False

# background lock so concurrent daily-checks don't race
_run_lock = threading.Lock()


@flask_app.route("/")
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
    return jsonify(repo)


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
    repo = registry.set_focus_branch(repo_id, branch)
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
    repo = registry.add_url(repo_id, name, url, branch)
    if not repo:
        abort(404)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>/rebase", methods=["POST"])
def api_rebase_upstream(repo_id, name):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    plan = upstream_check.build_merge_plan(repo["path"], repo["id"], repo["name"], repo["upstreams"])
    return jsonify({"ok": True, "plan": plan.to_dict()})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>/enable", methods=["POST"])
def api_enable_upstream(repo_id, name):
    data = request.get_json(force=True) or {}
    enabled = data.get("enabled", True)
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    for up in repo.get("upstreams", []):
        if up.get("name") == name:
            up["enabled"] = bool(enabled)
    store.save_repo(repo)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/repo/<repo_id>/upstream/<name>", methods=["DELETE"])
def api_remove_upstream(repo_id, name):
    repo = registry.remove_upstream(repo_id, name)
    if not repo:
        abort(404)
    return jsonify({"ok": True, "repo": repo})


@flask_app.route("/api/run-daily", methods=["POST"])
def api_run_daily():
    with _run_lock:
        repos = registry.get_watching_repos()
        results = []
        for repo in repos:
            try:
                plan = upstream_check.build_merge_plan(
                    repo["path"], repo["id"], repo["name"], repo["upstreams"],
                    focus_branch=repo.get("focus_branch"),
                )
                # decide auto-merge vs request
                if plan.safe:
                    # attempt auto-merge + push for each safe upstream
                    for up in plan.upstreams:
                        if up.conflict or up.up_to_date:
                            continue
                        # merge upstream into focus branch
                        merge = daily_check.merge_upstream(repo["path"], up)
                        if merge["ok"]:
                            results.append({
                                "repo_id": repo["id"],
                                "repo_name": repo["name"],
                                "upstream": up.name,
                                "status": "merged",
                                "ahead": up.ahead,
                                "behind": up.behind,
                                "push": merge,
                            })
                else:
                    # conflict -> raise a request
                    request_rec = {
                        "id": f"{repo['id']}:{plan.focus_branch}",
                        "repo_id": repo["id"],
                        "repo_name": repo["name"],
                        "focus_branch": plan.focus_branch,
                        "conflicts": plan.conflicts,
                        "upstreams": [u.to_dict() for u in plan.upstreams],
                        "actions": plan.actions,
                        "safe": plan.safe,
                        "status": "pending",
                        "created_at": config.now_iso(),
                        "comment": None,
                        "decided_at": None,
                    }
                    store.record_request(request_rec)
                    results.append({
                        "repo_id": repo["id"],
                        "repo_name": repo["name"],
                        "status": "request_raised",
                        "conflicts": plan.conflicts,
                    })
            except Exception as e:
                results.append({"repo_id": repo["id"], "status": "error", "error": str(e)})
    for r in results:
        _remember_daily(r)
    return jsonify({"ok": True, "results": results})


def _remember_daily(r: dict) -> None:
    status = r.get("status")
    if status == "merged":
        text = f"Daily check merged upstream {r.get('upstream')} ({r.get('behind')} commits behind, {r.get('ahead')} ahead)."
    elif status == "request_raised":
        text = f"Daily check found conflicts and raised an approval request: {', '.join(map(str, r.get('conflicts') or [])) or 'see request'}."
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
    data = request.get_json(force=True) or {}
    decision = data.get("decision")  # 'approve' | 'deny'
    comment = data.get("comment")
    if decision not in ("approve", "deny"):
        abort(400)
    result = store.decide_request(req_id, decision, comment)
    if not result:
        abort(404)
    try:
        memory.remember(result["repo_id"], (
            f"{config.now_iso()[:10]}: You {result['status']} the merge request for branch "
            f"{result.get('focus_branch')} (conflicts: {', '.join(map(str, result.get('conflicts') or [])) or 'none'})"
            + (f". Comment: {comment}" if comment else ".")), kind="decision")
    except Exception:
        pass
    return jsonify({"ok": True, "request": result})


@flask_app.route("/api/repo/<repo_id>/chat", methods=["POST"])
def api_chat(repo_id):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    data = request.get_json(force=True) or {}
    message = data.get("message")
    if not message:
        abort(400)
    from . import llm
    reply = llm.chat(repo_id, message, data.get("session"))
    return jsonify(reply)


@flask_app.route("/api/chat/<repo_id>")
def api_chat_history(repo_id):
    """Messages of one session (?session=<id>, default: the active one)."""
    if not registry.get_repo(repo_id):
        abort(404)
    session = store.get_session(repo_id, request.args.get("session"))
    return jsonify({"session": session["id"], "title": session.get("title"), "messages": session["messages"]})


@flask_app.route("/api/repo/<repo_id>/sessions")
def api_sessions(repo_id):
    if not registry.get_repo(repo_id):
        abort(404)
    return jsonify(store.list_sessions(repo_id))


@flask_app.route("/api/repo/<repo_id>/sessions", methods=["POST"])
def api_new_session(repo_id):
    """Start a fresh conversation (or switch with {"session": id}); memory is kept."""
    if not registry.get_repo(repo_id):
        abort(404)
    sid = (request.get_json(silent=True) or {}).get("session")
    session = store.set_active_session(repo_id, sid) if sid else store.new_session(repo_id)
    return jsonify({"ok": True, "session": session["id"]})


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
    """All memories of a repo, or a search with ?q= (&scope=all for every repo)."""
    if not registry.get_repo(repo_id):
        abort(404)
    q = request.args.get("q", "").strip()
    try:
        if not q:
            items = memory.list_repo(repo_id)
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
    """Store recent commits (subject + touched files) as memories so related
    changes, here and in other repos, can be found by meaning."""
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
                        kind="commit", extra={"sha": c["sha"]})
        done.add(c["sha"])
        added += 1
    repo["indexed_commits"] = sorted(done)
    store.save_repo(repo)
    return jsonify({"ok": True, "added": added, "total": len(done)})


@flask_app.route("/api/memory/<memory_id>", methods=["DELETE"])
def api_memory_delete(memory_id):
    memory.forget(memory_id)
    return jsonify({"ok": True})


@flask_app.route("/api/settings")
def api_settings():
    return jsonify(get_settings())


@flask_app.route("/api/settings", methods=["POST"])
def api_save_settings():
    data = request.get_json(force=True) or {}
    new = save_settings(data)
    if "memory" in data:
        memory.reload()
    # also mirror LLM config to hermes config.yaml so studio & this app share backend
    if "llm" in data:
        from . import hermes_config
        hermes_config.set_llm(data["llm"])
    return jsonify({"ok": True, "settings": new})


@flask_app.route("/api/files", methods=["GET"])
def api_files():
    repo_id = request.args.get("repo")
    path = request.args.get("path", "")
    repo = registry.get_repo(repo_id) if repo_id else None
    base = repo["path"] if repo else os.getcwd()
    if path:
        base = os.path.normpath(os.path.join(base, path))
    p = Path(base)
    if not p.exists() or not p.is_dir():
        abort(400)
    entries = []
    for child in p.iterdir():
        entries.append({
            "name": child.name,
            "type": "dir" if child.is_dir() else "file",
            "modified": child.stat().st_mtime,
        })
    return jsonify({"path": str(base), "entries": entries})


@flask_app.route("/api/files/contents", methods=["GET"])
def api_file_contents():
    repo_id = request.args.get("repo")
    path = request.args.get("path", "")
    repo = registry.get_repo(repo_id) if repo_id else None
    base = repo["path"] if repo else os.getcwd()
    full = os.path.normpath(os.path.join(base, path))
    if not full.startswith(base):
        abort(400)
    p = Path(full)
    if not p.is_file():
        abort(404)
    try:
        content = p.read_text()
    except UnicodeDecodeError:
        return jsonify({"error": "binary file"}), 400
    return jsonify({"path": str(p), "content": content})


@flask_app.route("/api/files/contents", methods=["POST"])
def api_file_write():
    data = request.get_json(force=True) or {}
    repo_id = data.get("repo")
    path = data.get("path")
    content = data.get("content", "")
    repo = registry.get_repo(repo_id) if repo_id else None
    base = repo["path"] if repo else os.getcwd()
    full = os.path.normpath(os.path.join(base, path))
    if not full.startswith(base):
        abort(400)
    p = Path(full)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return jsonify({"ok": True, "path": str(p)})


@flask_app.route("/api/branches")
def api_branches():
    path = request.args.get("repo", "")
    try:
        out = gitops._git(["branch", "-a"], path)
    except Exception:
        return jsonify({"branches": []})
    branches = [l.strip().lstrip("* ").strip() for l in out.splitlines() if l.strip()]
    return jsonify({"branches": branches})


@flask_app.route("/api/hermes-llm")
def api_hermes_llm():
    from . import hermes_config
    return jsonify(hermes_config.resolve_llm_config())


@flask_app.route("/api/models")
def api_models():
    from . import llm
    return jsonify({"data": [{"id": m} for m in llm.available_models()]})


TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Upstream Management</title>
<meta id="upstream-base" name="upstream-base" content="{{ base_url }}" />
<style>
  * { box-sizing: border-box; }
  body { margin: 0; font-family: system-ui, -apple-system, sans-serif; background: #0f1115; color: #e6e6e6; }
  header { background: #171a21; padding: 16px 24px; border-bottom: 1px solid #2a2f3a; display: flex; justify-content: space-between; align-items: center; }
  header h1 { margin: 0; font-size: 18px; }
  nav a { color: #9fb4ff; text-decoration: none; margin-left: 20px; font-size: 14px; }
  nav a:hover { color: #fff; }
  main { padding: 24px; max-width: 1200px; margin: 0 auto; }
  .repo-card { background: #171a21; border: 1px solid #2a2f3a; border-radius: 10px; padding: 18px; margin-bottom: 16px; }
  .repo-card h3 { margin: 0 0 8px; }
  .badge { display: inline-block; background: #232833; border: 1px solid #333a48; border-radius: 6px; padding: 2px 8px; font-size: 12px; margin-right: 6px; }
  .badge.up { background: #14301f; border-color: #235a3a; }
  .badge.warn { background: #332a14; border-color: #5a4a23; }
  .btn { background: #2f5bff; color: #fff; border: none; padding: 8px 14px; border-radius: 6px; cursor: pointer; font-size: 13px; }
  .btn:hover { background: #4a70ff; }
  .btn.danger { background: #b33; }
  .btn.ghost { background: #232833; border: 1px solid #333a48; }
  input, select, textarea { background: #0f1115; color: #e6e6e6; border: 1px solid #333a48; border-radius: 6px; padding: 8px; font-size: 13px; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .upstream { padding: 10px; border: 1px solid #2a2f3a; border-radius: 8px; margin-top: 8px; }
  .muted { color: #8892a0; font-size: 13px; }
  .chat-box { height: 300px; overflow-y: auto; border: 1px solid #2a2f3a; border-radius: 8px; padding: 12px; background: #0f1115; }
  .msg { margin-bottom: 10px; padding: 8px 12px; border-radius: 8px; max-width: 80%; }
  .msg.user { background: #2f5bff; margin-left: auto; }
  .msg.bot { background: #232833; white-space: pre-wrap; }
  .msg.err { background: #3a1d1d; }
  .msg { white-space: pre-wrap; }
  .mem-list { margin-top: 10px; max-height: 420px; overflow-y: auto; }
  .mem { display: flex; gap: 8px; align-items: flex-start; padding: 8px; border: 1px solid #2a2f3a; border-radius: 8px; margin-bottom: 6px; font-size: 13px; }
  .mem-del { padding: 2px 8px; }
  .badge.kind-commit { background: #1d2a40; border-color: #2c4366; }
  .badge.kind-event { background: #332a14; border-color: #5a4a23; }
  .badge.kind-decision { background: #2b1d40; border-color: #45306a; }
  .badge.kind-note { background: #14301f; border-color: #235a3a; }
  @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<header>
  <h1>🔗 Upstream Management</h1>
  <nav>
    <a href="{{ base_url }}/repos">Repos</a>
    <a href="{{ base_url }}/requests">Requests</a>
    <a href="{{ base_url }}/settings">LLM Settings</a>
  </nav>
</header>
<main>
  <h2 id="page-title">Watched Repositories</h2>
  <div id="content">Loading…</div>
</main>
<script src="{{ base_url }}/static/app.js"></script>
</body>
</html>
"""
