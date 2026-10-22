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
import threading
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, render_template_string, abort

from . import config, registry, store, upstream_check, daily_check, gitops, memory
from .store import settings as get_settings, save_settings

flask_app = Flask(__name__)
flask_app.config["JSON_SORT_KEYS"] = False

# background lock so concurrent daily-checks don't race
_run_lock = threading.Lock()

# Extra hosts (host:port) allowed to send changes, e.g. a proxy in front of the app.
_ALLOWED_HOSTS = {h.strip() for h in os.environ.get("UPSTREAM_ALLOWED_HOSTS", "").split(",") if h.strip()}


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


@flask_app.route("/api/repo/<repo_id>/commits")
def api_commits(repo_id):
    """Commits, paged. view=all (focus branch) | behind (upstream has, you lack) | ahead (only yours)."""
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    view = request.args.get("view", "all")
    focus = repo.get("focus_branch") or "HEAD"
    ups = {u["name"]: u for u in repo.get("upstreams", [])}
    up = ups.get(request.args.get("upstream", "")) or next(iter(ups.values()), None)
    upref = f"refs/remotes/{up['name']}/{up.get('branch') or focus}" if up else None
    if view in ("behind", "ahead") and not (upref and gitops.ref_exists(repo["path"], upref)):
        return jsonify({"ok": False, "error": "upstream not fetched yet - press Fetch", "items": []})
    rev = {"all": focus, "behind": f"{focus}..{upref}", "ahead": f"{upref}..{focus}"}.get(view, focus)
    skip = max(0, int(request.args.get("skip", 0)))
    limit = min(200, max(1, int(request.args.get("limit", 50))))
    items = gitops.list_commits(repo["path"], rev, skip, limit, request.args.get("q", "").strip())
    return jsonify({"ok": True, "items": items, "more": len(items) == limit})


@flask_app.route("/api/repo/<repo_id>/commit/<sha>")
def api_commit(repo_id, sha):
    repo = registry.get_repo(repo_id)
    if not repo:
        abort(404)
    try:
        return jsonify({"ok": True, **gitops.commit_detail(repo["path"], sha)})
    except gitops.GitError as e:
        return jsonify({"ok": False, "error": str(e)}), 404


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
    f = _repo_file(repo, request.args.get("path", ""))
    if f is None or not f.is_file():
        abort(404)
    if f.stat().st_size > 1_000_000:
        return jsonify({"ok": False, "error": "file is larger than 1 MB"})
    try:
        text = f.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return jsonify({"ok": False, "error": "binary file"})
    return jsonify({"ok": True, "path": request.args.get("path", ""), "content": text, "lines": text.count(chr(10)) + 1})


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
                    if up.missing_ref or up.behind == 0:
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
    return jsonify({"ok": True, "results": results})


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
        with _run_lock:
            outcome = [{"upstream": u["name"], **daily_check.merge_upstream(repo["path"], u, req["focus_branch"],
                                                                            allow_merge_commit=True)}
                       for u in req.get("upstreams", [])]
        failed = not all(o["ok"] for o in outcome)
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
    repo = registry.get_repo(repo_id)
    if not repo:
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
    if not registry.get_repo(repo_id):
        abort(404)
    session = store.snapshot(store.get_session(repo_id, request.args.get("session")))
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


@flask_app.route("/api/repo/<repo_id>/sessions/<session_id>", methods=["DELETE"])
def api_delete_session(repo_id, session_id):
    if not registry.get_repo(repo_id):
        abort(404)
    return jsonify({"ok": store.delete_session(repo_id, session_id), "active": store.get_session(repo_id)["id"]})


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
    with store.lock:
        repo["indexed_commits"] = sorted(done)
        store.save_repo(repo)
    cleaned = memory.cleanup(repo_id) if memory._settings()["auto_cleanup"] else None
    return jsonify({"ok": True, "added": added, "total": len(done), "cleaned": cleaned})


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
    data = request.get_json(force=True) or {}
    if isinstance(data.get("llm"), dict) and "api_key" in data["llm"]:
        data["llm"]["api_key"] = _unmask(data["llm"]["api_key"]) or ""
    save_settings(data)
    if "memory" in data or "llm" in data:  # mem0 bakes in the chat model for extraction
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
    if p is None or p == Path(repo["path"]).resolve() or ".git" in p.relative_to(Path(repo["path"]).resolve()).parts:
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
    models, error = llm.list_models(request.args.get("base_url"), _unmask(request.args.get("api_key")))
    return jsonify({"data": [{"id": m} for m in models], "error": error})


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
  header { background: #171a21; padding: 16px 24px; border-bottom: 1px solid #2a2f3a; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 8px; }
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
  input, select, textarea { background: #0f1115; color: #e6e6e6; border: 1px solid #333a48; border-radius: 6px; padding: 8px; font-size: 13px; max-width: 100%; }
  .btn:disabled { opacity: .6; cursor: progress; }
  :focus-visible { outline: 2px solid #7fa0ff; outline-offset: 1px; }
  .loading { color: #8892a0; font-size: 13px; padding: 8px; }
  .loading::before { content: ""; display: inline-block; width: 10px; height: 10px; margin-right: 8px; border: 2px solid #4a5468; border-top-color: #9fb4ff; border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -1px; }
  @keyframes spin { to { transform: rotate(360deg); } }
  .sr-only { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
  .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  .upstream { padding: 10px; border: 1px solid #2a2f3a; border-radius: 8px; margin-top: 8px; }
  .muted { color: #8892a0; font-size: 13px; }
  .chat-box { height: min(420px, 60vh) !important; overflow-y: auto; border: 1px solid #2a2f3a; border-radius: 8px; padding: 12px; background: #0f1115; }
  .msg { margin-bottom: 10px; padding: 8px 12px; border-radius: 8px; max-width: 80%; }
  .msg.user { background: #2f5bff; margin-left: auto; }
  .msg.bot { background: #232833; white-space: normal; overflow-wrap: anywhere; }
  .msg.bot p, .msg.bot ul, .msg.bot ol, .msg.bot blockquote { margin: 0 0 8px; }
  .msg.bot > :last-child { margin-bottom: 0; }
  .msg.bot ul, .msg.bot ol { padding-left: 20px; }
  .msg.bot h3, .msg.bot h4, .msg.bot h5, .msg.bot h6 { margin: 6px 0; font-size: 14px; }
  .msg.bot code { background: #11141a; border: 1px solid #2a2f3a; border-radius: 4px; padding: 0 4px; font: 12px ui-monospace, Consolas, monospace; }
  .msg.bot pre.md-code { background: #0f1115; border: 1px solid #2a2f3a; border-radius: 6px; padding: 8px; overflow: auto; margin: 0 0 8px; }
  .msg.bot pre.md-code code { background: none; border: 0; padding: 0; white-space: pre; }
  .msg.bot blockquote { border-left: 3px solid #3a4152; padding-left: 8px; color: #b8c0cc; }
  .msg.bot a { color: #9fb4ff; }
  .msg.err { background: #3a1d1d; }
  .msg.user { white-space: pre-wrap; overflow-wrap: anywhere; }
  .mem-list { margin-top: 10px; max-height: 420px; overflow-y: auto; }
  .mem { display: flex; gap: 8px; align-items: flex-start; padding: 8px; border: 1px solid #2a2f3a; border-radius: 8px; margin-bottom: 6px; font-size: 13px; }
  .mem-del { padding: 2px 8px; }
  .badge.kind-commit { background: #1d2a40; border-color: #2c4366; }
  .badge.kind-event { background: #332a14; border-color: #5a4a23; }
  .badge.kind-decision { background: #2b1d40; border-color: #45306a; }
  .badge.kind-note { background: #14301f; border-color: #235a3a; }
  .badge.bad { background: #3a1d1d; border-color: #6a2d2d; }
  .repo-grid { grid-template-columns: minmax(0, 1fr) minmax(0, 1.25fr); }
  .tabs { display: flex; gap: 4px; margin-bottom: 10px; flex-wrap: wrap; }
  .tab { background: #171a21; color: #cfd3dc; border: 1px solid #2a2f3a; border-radius: 6px 6px 0 0; padding: 6px 14px; cursor: pointer; }
  .tab.active { background: #232833; border-color: #3a4152; color: #fff; }
  .tray { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; margin-top: 8px; font-size: 12px; }
  .chip { background: #1d2a40; border: 1px solid #2c4366; border-radius: 12px; padding: 2px 4px 2px 10px; }
  .chip-x { background: none; border: none; color: #9fb4ff; cursor: pointer; font-size: 14px; }
  .overlay { position: fixed; inset: 0; background: rgba(0,0,0,.6); z-index: 900; display: flex; align-items: center; justify-content: center; padding: 12px; }
  .overlay-panel { background: #171a21; border: 1px solid #2a2f3a; border-radius: 10px; padding: 16px; width: min(1100px, 100%); max-height: 90vh; overflow: auto; }
  .code-pre { margin: 8px 0 0; font: 12px/18px ui-monospace, Consolas, monospace; background: #0f1115; border: 1px solid #2a2f3a; border-radius: 6px; overflow: auto; max-height: 55vh; }
  .dl { white-space: pre; padding: 0 8px; }
  .dl.add { background: #12301c; color: #9be3b0; }
  .dl.del { background: #3a1d1d; color: #f0a0a0; }
  .dl.hunk { color: #7fb0ff; }
  .dl.file { color: #e0c070; font-weight: 600; }
  .files { display: grid; grid-template-columns: 38% 62%; border: 1px solid #2a2f3a; border-radius: 8px; height: min(560px, 75vh); overflow: hidden; }
  .tree { overflow: auto; border-right: 1px solid #2a2f3a; font-size: 13px; padding: 4px 0; background: #11141a; }
  .trow { cursor: pointer; padding: 2px 8px; white-space: nowrap; }
  .trow:focus-visible { outline-offset: -2px; }
  .trow:hover { background: #1b2030; }
  .trow.sel { background: #25304a; }
  .viewer { display: flex; flex-direction: column; min-width: 0; background: #0f1115; }
  .viewer-head { display: flex; gap: 6px; align-items: center; padding: 6px 8px; border-bottom: 1px solid #2a2f3a; font-size: 13px; flex-wrap: wrap; }
  .code-body { flex: 1; overflow: auto; position: relative; }
  .code-inner { display: flex; position: relative; min-width: max-content; padding: 8px 0; }
  .gutter, .src { margin: 0; font: 12px/18px ui-monospace, Consolas, monospace; }
  .gutter { text-align: right; color: #566; padding: 0 10px; border-right: 1px solid #2a2f3a; user-select: none; position: sticky; left: 0; background: #0f1115; }
  .src { padding: 0 14px; tab-size: 4; }
  .hl { position: absolute; left: 0; right: 0; height: 18px; background: rgba(255, 210, 80, .18); pointer-events: none; }
  @media (max-width: 900px) { .grid, .repo-grid { grid-template-columns: 1fr; } }
  @media (max-width: 700px) {
    main { padding: 14px; }
    header { padding: 12px 14px; }
    nav a { margin-left: 0; margin-right: 14px; }
    .files { grid-template-columns: 1fr; grid-template-rows: 40% 60%; }
    .tree { border-right: 0; border-bottom: 1px solid #2a2f3a; }
    .msg { max-width: 92%; }
    .repo-card { padding: 12px; }
  }
</style>
</head>
<body>
<header>
  <h1>🔗 Upstream Management</h1>
  <nav aria-label="Main">
    <a href="{{ base_url }}/repos">Repos</a>
    <a href="{{ base_url }}/requests">Requests</a>
    <a href="{{ base_url }}/settings">LLM Settings</a>
  </nav>
</header>
<main>
  <h2 id="page-title">Watched Repositories</h2>
  <div id="content" aria-live="polite"><div class="loading">Loading…</div></div>
</main>
<script src="{{ base_url }}/static/md.js"></script>
<script src="{{ base_url }}/static/app.js"></script>
</body>
</html>
"""
