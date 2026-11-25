"""The Overseer: keeps the work moving. Plain rules, no model: it is quick, cheap and predictable.

Every few minutes (UPSTREAM_OVERSEER_MINUTES, default 10; 0 turns the loop off) it:

1. scores every repo: the priority you gave it, conflicts with its upstreams, how far behind it is,
   requests waiting for you (and for how long), auto-approval checks that failed, pinned submodules
   that moved where you watch, open bugs and urgent tasks, errors;
2. turns what it finds into tasks on the board (a conflict to resolve, a failed check to fix, a repo
   that errors), once each, and closes its own tasks when the problem is gone;
3. ranks the board: urgent first, then by priority, kind, age and how much the repo needs attention.
   A card you placed by hand stays where you put it;
4. takes back claims that ran out (an agent went quiet) so another agent can pick the task up;
5. tells you what needs you (requests waiting too long, urgent work nobody has), once each;
6. if you allow it (dispatch: herdr or studio), hands the next tasks to coding agents, up to max_agents
   at a time.

Agents ask it what to do: `next_task(agent)` gives the most important open task nobody has and claims it,
which is what an agent started by cron does first. `plan()` is the whole picture in words.
"""
from __future__ import annotations

import datetime as dt
import os
import threading
import time

from . import board, config, gitops, notify, registry, store, tasks

PRIORITY_WEIGHT = {"urgent": 1000, "high": 300, "normal": 100, "low": 20}
KIND_WEIGHT = {"bug": 40, "merge": 30, "fix": 20, "feature": 10, "chore": 5, "idea": -20}
REPO_PRIORITY = {"critical": 60, "high": 30, "normal": 0, "low": -20}
DEFAULTS = {"enabled": True, "make_tasks": True, "dispatch": "off", "max_agents": 2, "stale_request_days": 2,
            "urgent_unclaimed_minutes": 30, "ready_unsubmitted_minutes": 15}
_lock = threading.Lock()
SEND = None   # set by app.py: _send_task(task_id, to, message, opts) -> {ok, ...}


def settings() -> dict:
    s = {**DEFAULTS, **(store.settings().get("overseer") or {})}
    env = os.environ.get("UPSTREAM_OVERSEER_MINUTES")
    s["every_minutes"] = float(env) if env not in (None, "") else float(s.get("every_minutes", 10))
    return s


def save_settings(data: dict) -> dict:
    cur = dict(store.settings().get("overseer") or {})
    if "enabled" in data:
        cur["enabled"] = bool(data["enabled"])
    if "make_tasks" in data:
        cur["make_tasks"] = bool(data["make_tasks"])
    if "dispatch" in data:
        if data["dispatch"] not in ("off", "herdr", "studio"):
            raise ValueError("dispatch is off, herdr or studio")
        cur["dispatch"] = data["dispatch"]
    for k, lo, hi in (("max_agents", 0, 20), ("stale_request_days", 0.1, 60), ("every_minutes", 1, 1440),
                      ("urgent_unclaimed_minutes", 1, 1440), ("ready_unsubmitted_minutes", 1, 1440)):
        if k in data:
            try:
                cur[k] = max(lo, min(float(data[k]), hi))
            except (TypeError, ValueError):
                raise ValueError(f"{k} is a number")
    store.save_settings({"overseer": cur})
    return settings()


def _days(iso: str | None) -> float:
    try:
        return max(0.0, (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(iso)).total_seconds() / 86400)
    except (TypeError, ValueError):
        return 0.0


# ------------------------------------------------------------------ scoring
def repo_scores(summaries: list[dict] | None = None) -> list[dict]:
    summaries = summaries if summaries is not None else registry.repo_summary()
    reqs = [r for r in store.snapshot(store.get_requests()) if r.get("status") == "pending"]
    open_tasks = [t for t in tasks.list_tasks() if t["status"] in ("open", "working")]
    wiki_prio = wiki_priorities()
    out = []
    for r in summaries:
        repo = store.get_repo(r["id"]) or {}
        score, why = 0.0, []

        def add(n, reason):
            nonlocal score
            score += n
            why.append(reason)
        p = repo.get("priority") or "normal"
        if REPO_PRIORITY.get(p):
            add(REPO_PRIORITY[p], f"you marked it {p}")
        wp = wiki_prio["repos"].get(r["id"])
        if wp:
            add(wp["weight"], f"the wiki's priorities page: {wp['word']} (\"{wp['line'][:80]}\")")
        if r.get("error"):
            add(20, f"error: {str(r['error'])[:80]}")
        for u in r.get("upstream_details") or []:
            if u.get("enabled") is False or u.get("missing_ref"):
                continue
            if u.get("conflict"):
                add(25, f"conflicts with {u['name']}/{u.get('branch')}")
            if u.get("behind"):
                add(min(20, 4 + u["behind"] / 5), f"{u['behind']} behind {u['name']}")
        if r.get("dirty_tracked") and any((u.get("behind") or 0) for u in r.get("upstream_details") or []):
            add(10, "uncommitted changes block bringing upstream in")
        for c in r.get("components") or []:
            if c.get("behind") and (c.get("watched_changes") or c.get("local")):
                add(15, f"submodule {c['name']} moved where you watch")
            elif c.get("behind"):
                add(4, f"submodule {c['name']} is {c['behind']} behind")
        mine = [q for q in reqs if q.get("repo_id") == r["id"]]
        for q in mine:
            age = _days(q.get("created_at"))
            add(10 + min(20, 5 * age), f"request waiting {age:.1f} day(s)")
            if (q.get("auto") or {}).get("state") == "failed":
                add(30, "an auto-approval check failed")
        bugs = [t for t in open_tasks if t["repo_id"] == r["id"] and t["kind"] == "bug"]
        if bugs:
            add(8 * len(bugs), f"{len(bugs)} open bug(s)")
        urgent = [t for t in open_tasks if t["repo_id"] == r["id"] and t.get("priority") == "urgent"]
        if urgent:
            add(40, f"{len(urgent)} urgent task(s)")
        if (r.get("integration") or {}).get("state") in ("planned", "partial"):
            add(5, f"integration {(r.get('integration') or {}).get('state')}")
        out.append({"id": r["id"], "name": r.get("name") or r["id"], "priority": p, "score": round(score, 1), "why": why,
                    "groups": r.get("groups") or [], "paused": bool(wp and wp["word"] == "paused")})
    return sorted(out, key=lambda x: -x["score"])


def wiki_priorities() -> dict:
    """The user's priorities page in the LLM wiki (see wiki.priorities); empty when there is none or the wiki is off."""
    try:
        from . import wiki
        return wiki.priorities()
    except Exception:
        return {"note": None, "path": None, "repos": {}, "lines": []}


def paused_repos() -> set[str]:
    return {rid for rid, p in wiki_priorities()["repos"].items() if p["word"] == "paused"}


def task_score(t: dict, repo_score: float = 0.0) -> tuple[float, list[str]]:
    why = [f"{t.get('priority', 'normal')} priority"]
    s = PRIORITY_WEIGHT.get(t.get("priority", "normal"), 100) + KIND_WEIGHT.get(t["kind"], 0)
    age = _days(t.get("created_at"))
    if age >= 1:
        s += min(40, 2 * age)
        why.append(f"{age:.0f} day(s) old")
    if repo_score:
        s += 0.5 * repo_score
        why.append(f"repo needs attention ({repo_score:.0f})")
    if t.get("kind") == "idea":
        why.append("an idea: after real work")
    return round(s, 1), why


# ------------------------------------------------------------------ findings -> tasks
def findings(summaries: list[dict]) -> list[dict]:
    """Problems that are work for an agent, each with a stable key (so it becomes one task, once)."""
    out = []
    for r in summaries:
        repo = store.get_repo(r["id"]) or {}
        focus = repo.get("focus_branch") or "main"
        for u in r.get("upstream_details") or []:
            if u.get("enabled") is False or u.get("missing_ref") or not u.get("conflict"):
                continue
            out.append({"key": f"conflict:{r['id']}:{u['name']}:{u.get('upstream_sha')}", "repo": r["id"], "kind": "merge",
                        "priority": "high", "upstream": u["name"],
                        "title": f"Resolve the conflicts bringing {u['name']}/{u.get('branch')} into {focus}",
                        "body": f"{len(u.get('conflict_files') or [])} conflicting file(s): " + ", ".join((u.get("conflict_files") or [])[:20])
                                + "\n\nOpen a merge workspace (open_merge_workspace), resolve keeping both sides' intent, run the tests, "
                                  "submit it with a summary."})
        if r.get("error"):
            out.append({"key": f"error:{r['id']}:{str(r['error'])[:60]}", "repo": r["id"], "kind": "chore", "priority": "normal",
                        "title": f"Repo error: {str(r['error'])[:120]}", "body": str(r["error"])[:2000]})
    for q in store.snapshot(store.get_requests()):
        a = q.get("auto") or {}
        if q.get("status") == "pending" and a.get("state") == "failed" and q.get("repo_id"):
            out.append({"key": f"autofail:{q['id']}:{q.get('created_at')}", "repo": q["repo_id"], "kind": "fix", "priority": "high",
                        "title": f"Make the check pass: `{a.get('check') or 'check'}` fails for {q['id']}",
                        "body": "An auto-approval check failed, so this request waits for the user. Find out why (the output is "
                                "below), fix it in a change workspace, run the check there and submit.\n\n" + (a.get("detail") or "")[-3000:],
                        "about": [{"type": "request", "id": q["id"], "label": q["id"]}]})
    return out


def _own(t: dict) -> str | None:
    s = t.get("source") or {}
    return s.get("key") if s.get("type") == "overseer" else None


def _make_tasks(found: list[dict]) -> list[str]:
    actions = []
    existing = {_own(t): t for t in tasks.list_tasks() if _own(t)}
    keys = {f["key"] for f in found}
    for f in found:
        if f["key"] in existing or not store.get_repo(f["repo"]):
            continue
        try:
            t = tasks.create(f["repo"], f["title"], f["body"], f["kind"], f.get("about"), by="overseer",
                             upstream=f.get("upstream"), priority=f["priority"])
        except tasks.TaskError:
            continue
        with store.lock:
            t["source"] = {"type": "overseer", "key": f["key"]}
            store.save_state()
        actions.append(f"made task {t['id']}: {t['title']}")
    # its own tasks whose problem is gone are done (unless someone is on them)
    for key, t in existing.items():
        if key not in keys and t["status"] == "open" and not board.claim_alive(t):
            tasks.update(t["id"], status="done", note="the Overseer closed it: the problem is gone")
            actions.append(f"closed task {t['id']}: the problem is gone")
    return actions


# ------------------------------------------------------------------ one round
def run(reason: str = "scheduled") -> dict:
    if not _lock.acquire(blocking=False):
        return store.snapshot(store._ensure().get("overseer") or {})
    try:
        return _run(reason)
    finally:
        _lock.release()


def _run(reason: str) -> dict:
    s = settings()
    summaries = registry.repo_summary()
    scores = repo_scores(summaries)
    by_repo = {r["id"]: r["score"] for r in scores}
    actions = []
    if s["make_tasks"]:
        actions += _make_tasks(findings(summaries))

    # rank the board
    now = dt.datetime.now(dt.timezone.utc)
    with store.lock:
        for t in tasks.list_tasks():
            if t["status"] not in ("open", "working", "parked"):
                continue
            score, why = task_score(t, by_repo.get(t["repo_id"], 0))
            t["score"], t["why"] = score, why
            t["rank"] = t["manual_rank"] if t.get("manual_rank") is not None else score
        store.save_state()

    # claims that ran out: back to open for someone else
    for t in tasks.list_tasks(status="working"):
        c = t.get("claimed_by") or {}
        if c.get("agent") and not board.claim_alive(t, now):
            with store.lock:
                t["claimed_by"], t["status"] = None, "open"
                t["log"].append({"at": config.now_iso(), "text": f"the Overseer took it back: {c['agent']}'s claim ran out"})
                store.save_state()
            board.comment(t["id"], "overseer", f"{c['agent']} went quiet (claim ran out at {c.get('until', '')[11:16]} UTC); "
                                               "it is open again for any agent. See the log for how far it got.")
            notify.notify("stuck", f"Back on the board: {t['title'][:80]}", f"{c['agent']} stopped checking in.",
                          level="info", path=f"/work?task={t['id']}", key=f"lapsed:{t['id']}:{c.get('until')}", repo=t["repo_id"])
            actions.append(f"took back {t['id']} from {c['agent']}")

    # work that is done but never handed in: a task's workspace is ready, nothing was submitted, and its
    # agent has been quiet a while (some models stop right after saying what they'll do next)
    pending_ws = {q.get("workspace") for q in store.snapshot(store.get_requests()) if q.get("status") == "pending"}
    for repo in store.snapshot(store.all_repos()):
        from . import workspaces
        try:
            open_ws = workspaces.list_workspaces(repo)
        except Exception:
            continue
        for w in open_ws:
            tid = w.get("task")
            t = tasks.get(tid) if tid else None
            if not t or not w.get("ready") or w["name"] in pending_ws or t["status"] != "working":
                continue
            quiet = _days(t.get("updated_at")) * 1440
            if quiet < s.get("ready_unsubmitted_minutes", 15):
                continue
            agent = (t.get("claimed_by") or {}).get("agent")
            if agent and not str(agent).startswith("herdr:"):
                try:
                    board.post("overseer", agent, f"Your workspace {w['name']} for task {t['id']} ({t['title'][:80]}) is ready but "
                               "not submitted: call submit_merge_workspace with a summary, then release_task as done.", t["id"])
                except board.BoardError:
                    pass
            notify.notify("stuck", f"Ready but not submitted: {t['title'][:80]}",
                          f"workspace {w['name']} in {repo['id']} is ready; {agent or 'its agent'} went quiet. Submit it from the repo's "
                          "Merges tab, or nudge the agent.", level="info", path=f"/repo/{repo['id']}",
                          key=f"unsubmitted:{w['name']}:{w.get('head')}", repo=repo["id"])
            actions.append(f"nudged {agent or 'nobody'}: workspace {w['name']} ready but not submitted")

    # a herdr agent that finished but never said so: its status doesn't always reach us (herdr can't always
    # tell what an agent in a pane is doing), so look at the work itself: changes on the task's branch, the
    # commit message it proposes, and nothing new for a while
    from . import workspaces as _ws
    for t in tasks.list_tasks(status="working"):
        w = t.get("where") or {}
        if w.get("type") != "herdr" or not w.get("workspace") or w.get("status") in ("blocked",):
            continue
        repo = store.get_repo(t["repo_id"])
        try:
            path = _ws.path_of(repo, w["workspace"]) if repo else None
        except _ws.WorkspaceError:
            path = None
        msg = path / tasks.COMMIT_FILE if path else None
        if not msg or not msg.is_file() or msg.is_symlink():
            continue
        changed = [l for l in gitops._run(["git", "status", "--porcelain"], cwd=str(path)).stdout.splitlines()
                   if l[3:].strip() != tasks.COMMIT_FILE]
        if not changed and not gitops.behind_ahead(str(path), w.get("base") or (repo.get("focus_branch") or "main"), "HEAD")[0]:
            continue
        last = max(_days(t.get("updated_at")), 0) * 1440
        since_msg = (time.time() - msg.stat().st_mtime) / 60
        if min(last, since_msg) < s.get("ready_unsubmitted_minutes", 15):
            continue
        sent = notify.notify("stuck", f"Ready for your review: {t['title'][:80]}",
                             f"{w.get('agent') or 'The agent'} left changes and a proposed commit message, then went quiet. "
                             "Review and commit them on the task.", level="info", path=f"/work?task={t['id']}",
                             key=f"review:{t['id']}:{int(msg.stat().st_mtime)}", repo=t["repo_id"])
        if sent:
            board.comment(t["id"], "overseer", "The agent's changes and proposed commit message are ready and it has gone "
                                               "quiet: over to you to review and commit them.")
            actions.append(f"ready for review: {t['id']}")

    # what needs the user
    for q in store.snapshot(store.get_requests()):
        if q.get("status") == "pending" and _days(q.get("created_at")) >= s["stale_request_days"]:
            notify.notify("request", f"Still waiting for you: {q.get('repo_name') or q.get('repo_id')}",
                          f"request {q['id']} has waited {_days(q.get('created_at')):.1f} day(s)", level="high",
                          path=f"/requests#req-{q['id']}", key=f"stale:{q['id']}:{q.get('created_at')}", repo=q.get("repo_id"))
    top = sorted((t for t in tasks.list_tasks(status="open")), key=lambda t: -(t.get("rank") or 0))
    for t in top:
        if t.get("priority") == "urgent" and _days(t.get("created_at")) * 1440 >= s["urgent_unclaimed_minutes"]:
            notify.notify("overseer", f"Urgent and nobody is on it: {t['title'][:80]}", f"in {t['repo_id']}", level="urgent",
                          path=f"/work?task={t['id']}", key=f"urgent:{t['id']}", repo=t["repo_id"])

    # hand work out, if allowed
    if s["dispatch"] != "off" and SEND and s["max_agents"]:
        busy = [t for t in tasks.list_tasks(status="working") if (t.get("where") or {}).get("type") in ("herdr", "studio", "hermes")]
        free = int(s["max_agents"]) - len(busy)
        paused = {r["id"] for r in scores if r.get("paused")}
        for t in top:
            if free <= 0:
                break
            if t["kind"] not in tasks.CODE_KINDS or board.claim_alive(t) or t.get("where") or t["repo_id"] in paused:
                continue
            r = SEND(t["id"], s["dispatch"], None, {})
            if r.get("ok"):
                free -= 1
                actions.append(f"sent {t['id']} to {s['dispatch']}")
                board.comment(t["id"], "overseer", f"Sent to {s['dispatch']}: it was the most important open task nobody had.")
            else:
                actions.append(f"could not send {t['id']}: {r.get('error')}")
                break

    report = {"at": config.now_iso(), "reason": reason, "repos": scores[:50],
              "next": [{"id": t["id"], "title": t["title"], "repo": t["repo_id"], "priority": t.get("priority"),
                        "score": t.get("score"), "why": t.get("why")} for t in top[:15]],
              "working": [{"id": t["id"], "title": t["title"], "repo": t["repo_id"], "agent": (t.get("claimed_by") or {}).get("agent")
                           or (t.get("where") or {}).get("agent")} for t in tasks.list_tasks(status="working")][:30],
              "agents": board.agents(), "actions": actions,
              "wiki_priorities": {k: v for k, v in wiki_priorities().items() if k in ("note", "path", "lines")},
              "waiting_for_you": [{"id": q["id"], "repo": q.get("repo_id"), "days": round(_days(q.get("created_at")), 1)}
                                  for q in store.snapshot(store.get_requests()) if q.get("status") == "pending"],
              "settings": {k: s[k] for k in ("enabled", "make_tasks", "dispatch", "max_agents", "every_minutes")}}
    with store.lock:
        store._ensure()["overseer"] = report
        store.save_state()
    return report


def last() -> dict:
    return store.snapshot(store._ensure().get("overseer") or {})


# ------------------------------------------------------------------ for agents
def next_task(agent: str, repo: str | None = None, group: str | None = None, kinds: list[str] | None = None,
              minutes: int = board.LEASE_MINUTES) -> dict:
    """The most important open task nobody has, claimed for `agent`. With nothing to do: {task: None}."""
    agent = board.agent_name(agent)
    mine = [t for t in tasks.list_tasks(status="working") if (t.get("claimed_by") or {}).get("agent") == agent and board.claim_alive(t)]
    if mine:   # finish what you have first
        return {"task": mine[0], "already_yours": True, "brief": _brief(mine[0])}
    members = {r["id"] for r in store.all_repos() if group in (r.get("groups") or [])} if group else None
    if not last():
        run("first ask")
    paused = set() if repo else paused_repos()   # a repo the wiki says to pause: only when asked for by name
    pool = [t for t in tasks.list_tasks(status="open")
            if not board.claim_alive(t) and not (t.get("where") or {}).get("type") in ("herdr", "studio", "hermes")
            and t["repo_id"] not in paused
            and (not repo or t["repo_id"] == repo) and (members is None or t["repo_id"] in members)
            and (not kinds or t["kind"] in kinds) and t["kind"] != "idea"]
    pool.sort(key=lambda t: -(t.get("rank") if t.get("rank") is not None else task_score(t)[0]))
    for t in pool:
        try:
            claimed = board.claim(t["id"], agent, minutes)
        except board.BoardError:
            continue   # someone took it a moment ago
        return {"task": claimed, "already_yours": False, "brief": _brief(claimed)}
    return {"task": None, "brief": "Nothing open for you right now. Check read_messages, or ask again later."}


def _brief(t: dict) -> str:
    repo = store.get_repo(t["repo_id"])
    return (tasks.brief(t, repo) + "\n\n" + "Why now: " + "; ".join(t.get("why") or []) +
            f"\n\nPass task=\"{t['id']}\" to open_change_workspace so the workspace is linked to it. "
            "You have claimed it. Check in (heartbeat_task, a comment or update_task) at least every "
            f"{board.LEASE_MINUTES // 2} minutes or it goes back on the board. Work in a change or merge workspace, run the "
            "tests, submit it for approval, then release_task as done. Stuck? comment_task (to the user or another agent) "
            "and release it as parked.")


def plan(agent: str | None = None) -> str:
    """The picture in words, for an agent starting out."""
    rep = last() or run("asked for a plan")
    lines = ["# What matters now (from the Overseer)", f"As of {rep.get('at', '')[:16].replace('T', ' ')} UTC.", ""]
    if rep.get("repos"):
        lines.append("## Repos, most in need first")
        lines += [f"- {r['name']} ({r['id']}): {r['score']:.0f}" + (f" - {'; '.join(r['why'][:4])}" if r["why"] else " - fine")
                  for r in rep["repos"][:10]]
        lines.append("")
    wp = rep.get("wiki_priorities") or {}
    if wp.get("lines"):
        lines.append(f"## What the user wrote in the wiki ([[{wp['note']}]])")
        lines += [f"- {l['text']} ({l['word']}: {', '.join(l['repos'])})" for l in wp["lines"][:12]]
        lines.append("")
    if rep.get("next"):
        lines.append("## Open tasks, most important first (next_task gives you the top one nobody has)")
        lines += [f"- {t['id']} [{t['priority']}] {t['repo']}: {t['title']}" for t in rep["next"][:10]]
        lines.append("")
    if rep.get("working"):
        lines.append("## Being worked on")
        lines += [f"- {t['id']} {t['repo']}: {t['title']}" + (f" ({t['agent']})" if t.get("agent") else "") for t in rep["working"]]
        lines.append("")
    if rep.get("waiting_for_you"):
        lines.append(f"## Waiting for the user: {len(rep['waiting_for_you'])} request(s) (approving is theirs, not yours)")
        lines.append("")
    if agent:
        msgs = [m for m in store._ensure().get("messages", []) if m["to"] in (agent, "*") and agent not in m.get("read_by", [])]
        if msgs:
            lines.append(f"## You have {len(msgs)} unread message(s): read_messages")
            lines.append("")
    lines.append(HOW)
    return "\n".join(lines)


HOW = """## How agents work together here
- Take work with next_task (your name, e.g. "claude-1"): the most important open task nobody has, claimed for you.
  Or claim_task a specific one. One agent per task; a claimed task shows who has it.
- Check in at least every 30 minutes (heartbeat_task, comment_task or update_task), or your claim runs out
  and the Overseer puts the task back for someone else.
- Talk on the task (comment_task; `to` an agent or "user"), or message an agent (send_message; "*" is everyone).
  Read yours with read_messages when you start and between steps.
- Finish: submit the workspace for approval, then release_task as done (open if someone else should go on,
  parked if it is blocked: say why in a comment).
- Started by cron? next_task, do it, release it, and stop when it says there is nothing for you.
- Look things up in the user's LLM wiki with wiki_search / wiki_read (long-term notes: decisions, how things work).
  When you finish, your task (with its thread) is recorded there for you: write findings worth keeping as comments.
- Never approve, push or change the user's settings (auto-approval, release wait, notifications, Overseer)."""


# ------------------------------------------------------------------ the loop
_started = False


def start_background() -> None:
    global _started
    if _started:
        return
    _started = True

    def loop():
        time.sleep(20)   # let the app come up first
        while True:
            s = settings()
            if s["enabled"] and s["every_minutes"] > 0:
                try:
                    run()
                except Exception:   # never let one bad round stop it
                    pass
            time.sleep(max(60.0, s["every_minutes"] * 60) if s["every_minutes"] > 0 else 300)
    threading.Thread(target=loop, daemon=True, name="overseer").start()
