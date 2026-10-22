"""LLM chat over any OpenAI-compatible server (llama-swap, llama-server, Argos, ...).

The connection (base URL, model, optional API key) is the app's own setting,
kept in its state file and edited on the Settings page; it is independent of
hermes-agent. UPSTREAM_LLM_BASE_URL / UPSTREAM_LLM_MODEL / UPSTREAM_LLM_API_KEY
override it without touching the state file, which is how a dry run points the
app at a different model.

Every request is assembled from three layers:
  1. live repo facts (path, branches, upstreams, recent commits)
  2. long-term memory: relevant memories of this repo + similar ones from others
  3. the active chat session's recent turns
Starting a new session drops layer 3 only.
"""
from __future__ import annotations

import os

import requests

from pathlib import Path

from . import components, gitops, memory, store, upstream_check

# Rough character budget for replayed session turns (~4 chars per token).
HISTORY_CHARS = 24_000
MAX_TOKENS = 2048


def config() -> dict:
    """The LLM connection: env override, then the app's saved settings.

    With no model chosen, the first one the server lists is used.
    """
    local = store.load_llm_config()
    cfg = {
        "model": os.environ.get("UPSTREAM_LLM_MODEL") or local.get("model") or "",
        "base_url": (os.environ.get("UPSTREAM_LLM_BASE_URL") or local.get("base_url") or "").rstrip("/"),
        "api_key": os.environ.get("UPSTREAM_LLM_API_KEY") or local.get("api_key") or "",
    }
    if not cfg["model"]:
        models, _ = list_models(cfg["base_url"], cfg["api_key"])
        cfg["model"] = models[0] if models else ""
    return cfg


def _headers(api_key: str) -> dict:
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


SYSTEM_PROMPT = """You are the assistant inside Upstream Management, a web app that watches one git repository and its upstreams.

What you can and cannot do:
- You cannot run commands, open files or browse. You only know what is written in this message: the live repo facts, your memories, and any items the user attached (commits, files).
- Never offer to run git commands and never ask the user to paste output that the repo facts below already give you (ahead/behind counts, conflicting files, recent commits, working-tree state). Read the numbers from the facts and answer.
- If you need a file or commit that is not below, tell the user to attach it with the Tag button (Files and Commits panels next to the chat), then wait. Do not guess its contents.
- If a fact says an upstream is not fetched yet, tell the user to press Fetch on that upstream.
- Answer directly and concretely. Do not end every reply with a question. Ask at most one clarifying question, and only when you truly cannot proceed.
- When something needs doing in git (merge, rebase, resolving conflicts), give the exact commands for the user to run themselves, with the real branch and remote names from the facts.
- Use the memories when they help, and say so when you rely on one."""

ATTACH_CHARS = 8000
MAX_ATTACHMENTS = 6  # keep in step with MAX_TAGS in static/app.js


def repo_facts(repo: dict) -> str:
    path = repo.get("path", "")
    st = gitops.status_summary(path)
    focus = repo.get("focus_branch")
    lines = [
        f"Repository: {repo.get('name') or repo.get('id')} (id {repo.get('id')})",
        f"Local path: {path}",
        f"Focus branch: {focus} (checked out: {st['branch']})",
        "Working tree: " + (f"{st['dirty']} changed file(s), e.g. {', '.join(st['changed'][:8])}" if st["dirty"] else "clean"),
    ]
    if repo.get("remote_url"):
        lines.append(f"Origin: {repo['remote_url']}")
    for u in repo.get("upstreams") or []:
        if not u.get("enabled", True):
            lines.append(f"Upstream {u.get('name')}: {u.get('url')} (disabled)")
            continue
        try:
            c = upstream_check.compare_upstream(path, u, focus)
        except Exception as e:  # keep chatting even if one upstream is broken
            lines.append(f"Upstream {u.get('name')}: {u.get('url')} - comparison failed: {e}")
            continue
        if c.missing_ref:
            lines.append(f"Upstream {c.name}: {c.url} branch {c.branch} - NOT FETCHED YET (press Fetch to compare)")
            continue
        line = (f"Upstream {c.name}: {c.url} branch {c.branch} - {focus} is {c.behind} commit(s) BEHIND it "
                f"(it has {c.behind} commits you lack) and {c.ahead} commit(s) AHEAD of it")
        if c.behind and c.ahead:
            line += (f"; a merge would conflict in {len(c.conflict_files)} file(s): {', '.join(c.conflict_files[:15])}"
                     if c.conflict else "; a merge would apply cleanly (no conflicts)")
        elif c.up_to_date:
            line += "; up to date"
        lines.append(line)
        if c.behind:
            missing = gitops.list_commits(path, f"{focus}..refs/remotes/{c.name}/{c.branch}", 0, 8)
            if missing:
                lines.append("  newest commits you are missing: " + "; ".join(f"{m['sha']} {m['subject']}" for m in missing))
    lines += _component_facts(repo)
    try:
        top = sorted(p.name + ("/" if p.is_dir() else "") for p in Path(path).iterdir() if p.name != ".git")
        lines.append("Top-level files: " + ", ".join(top[:60]))
    except OSError:
        pass
    commits = gitops.recent_commits(path, 10)
    if commits:
        lines.append("Recent commits on your branch:")
        lines += [f"  {c['sha']} {c['date'][:10]} {c['subject']}" for c in commits]
    return "\n".join(lines)


def _component_facts(repo: dict) -> list[str]:
    """One line per submodule component, so questions about the parts get real numbers."""
    comps = repo.get("components") or []
    if not comps:
        return []
    states = components.compare_all(repo)
    # Name the exact set first: small models otherwise invent extra apps from commit subjects.
    lagging = [s for s in states if s.get("behind")]
    out = [f"Overall: the upstream lines above are about this repo's own code only. Of its {len(comps)} submodule "
           f"components, {len(lagging)} are behind their upstreams ({sum(s['behind'] for s in lagging)} commits in total)"
           + (", so the repo as a whole is NOT up to date." if lagging else "."),
           f"Submodule components: exactly {len(comps)} ({', '.join(s['name'] for s in states)}). "
           "Each is pinned to one commit and tracks its own upstream:"]
    for s in states:
        head = f"  {s['name']}" + (f" (path {s['path']})" if s["path"] != s["name"] else "") + f" <- {s['url']}"
        if s["status"] == "disabled":
            out.append(head + " (not tracked)")
        elif s["status"] in ("not_checked_out", "not_fetched", "error"):
            out.append(head + f" - {s['detail']}")
        elif s["status"] == "up_to_date":
            out.append(head + f" branch {s['branch']}: pinned {s['pin'][:8]}, up to date with upstream")
        else:
            out.append(head + f" branch {s['branch']}: pinned {s['pin'][:8]}, {s['behind']} commit(s) BEHIND upstream "
                       f"{(s.get('upstream') or '')[:8]}"
                       + (f"; WARNING: the pin also has {s['local']} commit(s) of the user's own that are not upstream"
                          if s["local"] else "; no local changes in the pin, so it can move straight to upstream"))
            if s["missing"]:
                out.append("    example upstream changes not in the pin: " + "; ".join(m["subject"] for m in s["missing"][:3]))
            if s["watched_changes"]:
                out.append(f"    WARNING: upstream changed {len(s['watched_changes'])} file(s) this repo copies or depends on: "
                           f"{', '.join(s['watched_changes'][:8])}. Bumping {s['name']} needs follow-up work"
                           + (f": run `{s['after_update']}`" if s["after_update"] else "") + ".")
    behind = [s for s in states if s.get("behind")]
    if behind:
        worst = max(behind, key=lambda s: s["behind"])
        out.append(f"  Most behind: {worst['name']} ({worst['behind']}); least behind: "
                   f"{min(behind, key=lambda s: s['behind'])['name']} ({min(s['behind'] for s in behind)}).")
    out.append("  How updating works in this app: pins never move on their own. 'Request update' at the top of the "
               "Components panel covers every component that is behind; the 'Request update' on one component's row covers "
               "only that one. The user approves it on the Requests page, and the app moves those pins straight to their "
               "upstream's latest commit in ONE local commit. Components whose pin has the user's own commits are skipped. "
               "Nothing is pushed, and follow-up commands are only shown to the user, never run by the app.")
    return out


def attachments_block(repo: dict, attachments: list[dict]) -> str:
    """Expand the commits/files the user tagged into text the model can read."""
    parts = []
    for a in attachments[:MAX_ATTACHMENTS]:
        try:
            if a.get("type") == "commit":
                d = gitops.commit_detail(repo["path"], a["id"], ATTACH_CHARS)
                parts.append(f"[Attached commit {d['sha'][:10]} by {d['author']} on {d['date'][:10]}]\n{d['message']}\n{d['stat']}\n"
                             f"{d['patch']}" + ("\n...(patch truncated)" if d["truncated"] else ""))
            elif a.get("type") == "file":
                root = Path(repo["path"]).resolve()
                f = (root / a["id"]).resolve()
                if root not in f.parents or not f.is_file():
                    continue
                text = f.read_text(encoding="utf-8", errors="replace")
                body = text[:ATTACH_CHARS] + ("\n...(file truncated)" if len(text) > ATTACH_CHARS else "")
                parts.append(f"[Attached file {a['id']}]\n{body}")
        except Exception as e:
            parts.append(f"[Attachment {a.get('id')} could not be read: {e}]")
    return "\n\n".join(parts)


# Cosine similarity cut-offs for memories put in the prompt. Measured with
# bge-small: unrelated cross-repo items score 0.36-0.63, real matches 0.74+.
OWN_MIN_SCORE = 0.3
OTHER_REPO_MIN_SCORE = 0.7


def _relevant(m: dict, minimum: float) -> bool:
    return m.get("score") is None or m["score"] >= minimum


def _memory_block(repo_id: str, message: str) -> str:
    # Commits are far more numerous than notes and preferences, so they would crowd
    # the latter out of one shared top-k. Search the two groups separately.
    own = [m for m in memory.search(message, repo_id=repo_id, kind="commit", k=5)
           + memory.search(message, repo_id=repo_id, exclude_kind="commit", k=5) if _relevant(m, OWN_MIN_SCORE)]
    # Other repos' memories only when they are clearly about the same thing;
    # loosely similar ones (another repo's "commits behind" chat) mislead the model.
    other = [m for m in memory.search(message, exclude=repo_id, k=4) if _relevant(m, OTHER_REPO_MIN_SCORE)]
    parts = []
    if own:
        parts.append("What you remember about this repo:\n" + "\n".join(f"- [{m['kind']}] {m['memory']}" for m in own))
    if other:
        parts.append("Possibly related, from other watched repos:\n" + "\n".join(
            f"- ({m['repo']}, {m['kind']}) {m['memory']}" for m in other))
    return "\n\n".join(parts)


def _history(session: dict) -> list[dict]:
    """Most recent turns of the session that fit the character budget."""
    out, used = [], 0
    for m in reversed(session.get("messages", [])):
        if m.get("error"):
            continue
        used += len(m["content"])
        if used > HISTORY_CHARS:
            break
        out.append({"role": m["role"], "content": m["content"]})
    return list(reversed(out))


def build_messages(repo: dict, session: dict, message: str, attachments: list[dict] | None = None) -> list[dict]:
    system = SYSTEM_PROMPT + "\n\n" + repo_facts(repo)
    mem = _memory_block(repo["id"], message)
    if mem:
        system += "\n\n" + mem
    att = attachments_block(repo, attachments or [])
    if att:
        system += "\n\nThe user attached these for this question:\n\n" + att
    return [{"role": "system", "content": system}, *_history(session), {"role": "user", "content": message}]


def chat(repo_id: str, message: str, session_id: str | None = None, attachments: list[dict] | None = None) -> dict:
    """Send a chat message to the LLM and return {ok, reply, error, session}."""
    repo = store.get_repo(repo_id)
    if not repo:
        return {"ok": False, "error": "repo not found"}
    session = store.get_session(repo_id, session_id)
    cfg = config()
    messages = build_messages(repo, session, message, attachments)
    store.add_chat_message(repo_id, session["id"], "user", message, attachments=attachments or None)
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json={
                "model": cfg["model"], "messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0.5,
                # Reasoning models (Gemma 4, Qwen3.5) otherwise spend the whole budget
                # thinking; llama-server honours this per request, others ignore it.
                "chat_template_kwargs": {"enable_thinking": False},
            },
            headers=_headers(cfg["api_key"]), timeout=300,
        )
        resp.raise_for_status()
        choice = resp.json()["choices"][0]
        reply = (choice["message"].get("content") or "").strip()
        if not reply:
            raise ValueError(f"model returned no answer (finish_reason={choice.get('finish_reason')}); "
                             "it may have used all tokens on reasoning")
    except Exception as e:  # network / parse / API error / empty answer
        store.add_chat_message(repo_id, session["id"], "assistant", f"[LLM error: {e}]", error=True)
        return {"ok": False, "error": str(e), "reply": "", "session": session["id"]}

    store.add_chat_message(repo_id, session["id"], "assistant", reply)
    memory.learn_from_chat(repo_id, message, reply)
    return {"ok": True, "reply": reply, "session": session["id"]}


def list_models(base_url: str | None = None, api_key: str | None = None) -> tuple[list[str], str | None]:
    """Model ids the server offers, as (ids, error). Defaults to the saved connection."""
    if base_url is None:
        local = store.load_llm_config()
        base_url = os.environ.get("UPSTREAM_LLM_BASE_URL") or local.get("base_url") or ""
        api_key = os.environ.get("UPSTREAM_LLM_API_KEY") or local.get("api_key") or ""
    try:
        resp = requests.get(f"{base_url.rstrip('/')}/models", headers=_headers(api_key or ""), timeout=10)
        resp.raise_for_status()
        return [m["id"] for m in resp.json().get("data", []) if m.get("id")], None
    except Exception as e:
        return [], str(e)
