"""LLM chat via the hermes-agent backend (llama-swap, OpenAI-compatible).

Shares the same provider/base_url/model as Studio. Each call hits the
llama-swap `/v1/chat/completions` endpoint with the active LLM config from the
hermes config.yaml (or the local app settings fallback).
"""
from __future__ import annotations

import requests

from . import hermes_config, store


def _config() -> dict:
    """Return the LLM config: prefer local settings, then hermes config.yaml."""
    local = store.load_llm_config()
    hermes_llm = hermes_config.resolve_llm_config()
    return {
        "model": local.get("model") or hermes_llm.get("model"),
        "base_url": local.get("base_url") or hermes_llm.get("base_url"),
        "api_mode": local.get("api_mode") or hermes_llm.get("api_mode"),
    }


def _messages(repo_id: str, message: str, focus_branch: str) -> list[dict]:
    return [
        {
            "role": "system",
            "content": (
                "You are Upstream Management's assistant for a single repository. "
                f"Repo focus branch: {focus_branch}.\n"
                "Answer repo-related questions concisely. When asked about upstreams, "
                "suggest concrete changes via a merge request."
            ),
        },
        {"role": "user", "content": message},
    ]


def chat(repo_id: str, message: str, focus_branch: str | None = None) -> dict:
    """Send a chat message to the LLM and return {ok, reply, error}."""
    repo = store.get_repo(repo_id)
    if not repo:
        return {"ok": False, "error": "repo not found"}
    if focus_branch is None:
        focus_branch = repo.get("focus_branch", "main")

    cfg = _config()
    url = f"{cfg['base_url']}/chat/completions"

    try:
        resp = requests.post(
            url,
            json={
                "model": cfg["model"],
                "messages": _messages(repo_id, message, str(focus_branch)),
                "max_tokens": 1024,
                "temperature": 0.5,
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
        reply = data["choices"][0]["message"]["content"].strip()
    except Exception as e:  # network / parse / API error
        store.add_chat_message(repo_id, "assistant", f"[LLM error: {e}]")
        return {"ok": False, "error": str(e), "reply": ""}

    # persist
    store.add_chat_message(repo_id, "user", message)
    store.add_chat_message(repo_id, "assistant", reply)
    return {"ok": True, "reply": reply}


def available_models() -> list[str]:
    """List the model names available from llama-swap."""
    cfg = _config()
    try:
        resp = requests.get(f"{cfg['base_url']}/models", timeout=10)
        resp.raise_for_status()
        return [m.get("id") for m in resp.json().get("data", [])]
    except Exception:
        return []
