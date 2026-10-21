"""LLM chat via the hermes-agent backend (llama-swap, OpenAI-compatible).

Shares the same provider/base_url/model as Studio. Each call hits the
`/v1/chat/completions` endpoint with the active LLM config from the hermes
config.yaml (or the local app settings fallback). UPSTREAM_LLM_BASE_URL /
UPSTREAM_LLM_MODEL override both without touching either file, which is how a
dry run points the app at a different model.

Every request is assembled from three layers:
  1. live repo facts (path, branches, upstreams, recent commits)
  2. long-term memory: relevant memories of this repo + similar ones from others
  3. the active chat session's recent turns
Starting a new session drops layer 3 only.
"""
from __future__ import annotations

import os

import requests

from . import gitops, hermes_config, memory, store

# Rough character budget for replayed session turns (~4 chars per token).
HISTORY_CHARS = 24_000


def config() -> dict:
    """Return the LLM config: env override, then local settings, then hermes config.yaml."""
    local = store.load_llm_config()
    hermes_llm = hermes_config.resolve_llm_config()
    return {
        "model": os.environ.get("UPSTREAM_LLM_MODEL") or local.get("model") or hermes_llm.get("model"),
        "base_url": (os.environ.get("UPSTREAM_LLM_BASE_URL") or local.get("base_url") or hermes_llm.get("base_url") or "").rstrip("/"),
        "api_mode": local.get("api_mode") or hermes_llm.get("api_mode"),
    }


def repo_facts(repo: dict) -> str:
    path = repo.get("path", "")
    lines = [
        f"Repository: {repo.get('name') or repo.get('id')} (id {repo.get('id')})",
        f"Local path: {path}",
        f"Focus branch: {repo.get('focus_branch')} (checked out: {gitops.current_branch(path)})",
    ]
    if repo.get("remote_url"):
        lines.append(f"Origin: {repo['remote_url']}")
    for u in repo.get("upstreams") or []:
        state = "enabled" if u.get("enabled", True) else "disabled"
        lines.append(f"Upstream {u.get('name')}: {u.get('url')} branch {u.get('branch')} ({state})")
    commits = gitops.recent_commits(path, 10)
    if commits:
        lines.append("Recent commits:")
        lines += [f"  {c['sha']} {c['date'][:10]} {c['subject']}" for c in commits]
    return "\n".join(lines)


def _memory_block(repo_id: str, message: str) -> str:
    own = memory.search(message, repo_id=repo_id, k=8)
    other = memory.search(message, exclude=repo_id, k=4)
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


def build_messages(repo: dict, session: dict, message: str) -> list[dict]:
    system = (
        "You are Upstream Management's assistant for a single repository. "
        "Answer repo questions concisely and concretely. When asked about upstreams, "
        "suggest concrete changes via a merge request. Use the memories below when "
        "they help, and say so when you are relying on one.\n\n"
        + repo_facts(repo)
    )
    mem = _memory_block(repo["id"], message)
    if mem:
        system += "\n\n" + mem
    return [{"role": "system", "content": system}, *_history(session), {"role": "user", "content": message}]


def chat(repo_id: str, message: str, session_id: str | None = None) -> dict:
    """Send a chat message to the LLM and return {ok, reply, error, session}."""
    repo = store.get_repo(repo_id)
    if not repo:
        return {"ok": False, "error": "repo not found"}
    session = store.get_session(repo_id, session_id)
    cfg = config()
    messages = build_messages(repo, session, message)
    store.add_chat_message(repo_id, session["id"], "user", message)
    try:
        resp = requests.post(
            f"{cfg['base_url']}/chat/completions",
            json={"model": cfg["model"], "messages": messages, "max_tokens": 1024, "temperature": 0.5},
            timeout=300,
        )
        resp.raise_for_status()
        reply = resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:  # network / parse / API error
        store.add_chat_message(repo_id, session["id"], "assistant", f"[LLM error: {e}]", error=True)
        return {"ok": False, "error": str(e), "reply": "", "session": session["id"]}

    store.add_chat_message(repo_id, session["id"], "assistant", reply)
    memory.learn_from_chat(repo_id, message, reply)
    return {"ok": True, "reply": reply, "session": session["id"]}


def available_models() -> list[str]:
    """List the model names available from the LLM server."""
    cfg = config()
    try:
        resp = requests.get(f"{cfg['base_url']}/models", timeout=10)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]
    except Exception:
        return []
