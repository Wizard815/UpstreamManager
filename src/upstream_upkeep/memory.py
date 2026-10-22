"""Long-term repo memory backed by mem0 (local Qdrant + embeddings).

Every memory belongs to one repo (metadata ``repo``) under a single mem0
user, so a search can stay inside one repo or span all of them (cross-repo
"has this change happened elsewhere?" lookups).

Memories come from three places:
  * chat turns      -> mem0 extracts durable facts with the chat LLM (infer)
  * events          -> daily-check results, approval decisions (stored verbatim)
  * commit indexing -> recent commit subjects + touched files (stored verbatim)

Chat sessions are separate (see store.py): resetting a session clears the
conversation window but keeps everything here.

Config (env wins over settings["memory"]):
  UPSTREAM_EMBED_PROVIDER  fastembed (default, in-process CPU) | openai
  UPSTREAM_EMBED_MODEL     e.g. BAAI/bge-small-en-v1.5, or the server's model name
  UPSTREAM_EMBED_BASE_URL  OpenAI-compatible /v1 base for provider=openai
  UPSTREAM_MEMORY=off      disable memory entirely
"""
from __future__ import annotations

import logging
import os
import re
import threading
from typing import Any

# mem0 sends usage telemetry to PostHog unless told not to.
os.environ.setdefault("MEM0_TELEMETRY", "False")

from . import config, store

log = logging.getLogger(__name__)

USER = "upkeep"
_lock = threading.Lock()
# Embedded Qdrant is not safe for concurrent use; Waitress threads and the
# background chat extraction all go through this.
_io = threading.RLock()
_COMMIT_FACT = re.compile(r"\b[0-9a-f]{8}\b")
_mem = None
_error: str | None = None


def _settings() -> dict:
    s = store.settings().get("memory", {})
    provider = os.environ.get("UPSTREAM_EMBED_PROVIDER") or s.get("embed_provider") or "fastembed"
    default_model = "BAAI/bge-small-en-v1.5" if provider == "fastembed" else ""
    return {
        "enabled": (os.environ.get("UPSTREAM_MEMORY") or s.get("enabled", "on")) != "off",
        "embed_provider": provider,
        "embed_model": os.environ.get("UPSTREAM_EMBED_MODEL") or s.get("embed_model") or default_model,
        "embed_base_url": os.environ.get("UPSTREAM_EMBED_BASE_URL") or s.get("embed_base_url") or "",
        "auto_cleanup": (os.environ.get("UPSTREAM_MEMORY_AUTOCLEAN") or s.get("auto_cleanup", "on")) != "off",
    }


def _embed_dims(cfg: dict) -> int:
    if cfg["embed_provider"] == "fastembed":
        from fastembed import TextEmbedding
        for m in TextEmbedding.list_supported_models():
            if m["model"] == cfg["embed_model"]:
                return int(m["dim"])
        raise ValueError(f"unknown fastembed model {cfg['embed_model']!r}")
    # OpenAI-compatible server: ask it once.
    from openai import OpenAI
    client = OpenAI(base_url=cfg["embed_base_url"], api_key="none")
    return len(client.embeddings.create(model=cfg["embed_model"], input="probe").data[0].embedding)


def _build():
    from mem0 import Memory
    from . import llm

    cfg = _settings()
    chat = llm.config()
    data_dir = config.CONFIG_PATH.parent / "mem0"
    data_dir.mkdir(parents=True, exist_ok=True)
    dims = _embed_dims(cfg)
    if cfg["embed_provider"] == "fastembed":
        embedder = {"provider": "fastembed", "config": {"model": cfg["embed_model"]}}
    else:
        # No embedding_dims here: mem0 would send `dimensions`, which most
        # local servers reject.
        embedder = {"provider": "openai", "config": {
            "model": cfg["embed_model"], "openai_base_url": cfg["embed_base_url"], "api_key": "none"}}
    # One collection per embedding model so switching models never mixes vector sizes.
    slug = re.sub(r"[^a-z0-9]+", "_", cfg["embed_model"].lower()).strip("_")
    return Memory.from_config({
        "llm": {"provider": "openai", "config": {
            "model": chat["model"], "openai_base_url": chat["base_url"], "api_key": chat["api_key"] or "none",
            "temperature": 0.1, "max_tokens": 1024}},
        "embedder": embedder,
        "vector_store": {"provider": "qdrant", "config": {
            "path": str(data_dir / "qdrant"), "on_disk": True,
            "collection_name": f"upkeep_{slug}", "embedding_model_dims": dims}},
        "history_db_path": str(data_dir / "history.db"),
    })


def get():
    """Return the mem0 instance, or None if memory is off or failed to start."""
    global _mem, _error
    with _lock:
        if _mem is None and _error is None:
            if not _settings()["enabled"]:
                _error = "memory disabled"
            else:
                try:
                    _mem = _build()
                except Exception as e:  # missing model, server down, bad config
                    _error = f"{type(e).__name__}: {e}"
                    log.warning("memory unavailable: %s", _error)
        return _mem


def reload() -> dict:
    """Drop the instance so the next call rebuilds it with current settings."""
    global _mem, _error
    with _lock:
        if _mem is not None:
            try:
                with _io:
                    _mem.close()
            except Exception:
                pass
        _mem, _error = None, None
    get()
    return status()


def status() -> dict:
    return {"ok": _mem is not None, "error": _error, **_settings()}


def _fmt(r: dict) -> dict:
    md = r.get("metadata") or {}
    return {
        "id": r.get("id"),
        "memory": r.get("memory"),
        "repo": md.get("repo"),
        "kind": md.get("kind", "chat"),
        "score": r.get("score"),
        "created_at": r.get("created_at"),
    }


def remember(repo_id: str, text: str, kind: str, extra: dict | None = None) -> list[dict]:
    """Store `text` verbatim (no LLM call) as a memory of `repo_id`."""
    m = get()
    if m is None or not text.strip():
        return []
    with _io:
        res = m.add(text, user_id=USER, infer=False, metadata={"repo": repo_id, "kind": kind, **(extra or {})})
    return [_fmt(r) for r in res.get("results", [])]


def learn_from_chat(repo_id: str, user_msg: str, reply: str) -> None:
    """Let mem0 pull durable facts out of a chat turn, in the background.

    It is one more LLM call, so it must not hold up the chat reply. Only the
    user's message is extracted from: the reply restates repo facts (commits
    the index already holds) and would be saved back as duplicate memories.
    """
    def run():
        m = get()
        if m is None:
            return
        try:
            with _io:
                res = m.add([{"role": "user", "content": user_msg}],
                            user_id=USER, metadata={"repo": repo_id, "kind": "chat"})
                # Small models still restate commits they were shown; the commit index
                # already holds those, so drop any such fact instead of keeping a duplicate.
                for r in res.get("results", []):
                    if r.get("id") and _COMMIT_FACT.search(r.get("memory") or ""):
                        m.delete(r["id"])
            if _settings()["auto_cleanup"]:
                cleanup(repo_id)
        except Exception as e:
            log.warning("memory extraction failed for %s: %s", repo_id, e)
    threading.Thread(target=run, daemon=True).start()


def search(query: str, repo_id: str | None = None, exclude: str | None = None, k: int = 8,
           kind: str | None = None, exclude_kind: str | None = None) -> list[dict]:
    m = get()
    if m is None or not query.strip():
        return []
    filters: dict[str, Any] = {"user_id": USER}
    if repo_id:
        filters["repo"] = repo_id
    elif exclude:
        filters["repo"] = {"ne": exclude}
    if kind:
        filters["kind"] = kind
    elif exclude_kind:
        filters["kind"] = {"ne": exclude_kind}
    with _io:
        res = m.search(query, filters=filters, top_k=k)
    return [_fmt(r) for r in res.get("results", [])]


def list_repo(repo_id: str, k: int = 200) -> list[dict]:
    m = get()
    if m is None:
        return []
    with _io:
        res = m.get_all(filters={"user_id": USER, "repo": repo_id}, top_k=k)
    items = [_fmt(r) for r in res.get("results", [])]
    return sorted(items, key=lambda r: r.get("created_at") or "", reverse=True)


def forget(memory_id: str) -> None:
    m = get()
    if m is not None:
        with _io:
            m.delete(memory_id)


_WORDS = re.compile(r"[a-z0-9]+")
_COMMIT_SHA = re.compile(r"^Commit ([0-9a-f]{7,40}) ")
MAX_EVENTS = 50


def _similar(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.8


def cleanup(repo_id: str) -> dict:
    """Prune a repo's memories: junk, exact and near duplicates, stale events.

    Oldest copy wins, so a memory keeps its original date. Safe to run any time;
    it runs automatically after chat extraction and commit indexing.
    """
    if get() is None:
        return {"removed": 0, "kept": 0, "reasons": {}}
    items = sorted(list_repo(repo_id, k=2000), key=lambda r: r.get("created_at") or "")  # oldest first
    drop: dict[str, str] = {}
    seen_sha: set[str] = set()
    kept_words: dict[str, list[set]] = {}
    events: list[dict] = []
    for it in items:
        text = (it.get("memory") or "").strip()
        kind = it.get("kind") or "chat"
        words = set(_WORDS.findall(text.lower()))
        if len(text) < 8:
            drop[it["id"]] = "empty"
        elif kind == "chat" and _COMMIT_FACT.search(text):
            drop[it["id"]] = "restated commit"
        elif kind == "commit" and (m := _COMMIT_SHA.match(text)):
            if m.group(1) in seen_sha:
                drop[it["id"]] = "duplicate commit"
            seen_sha.add(m.group(1))
        elif kind == "event":
            events.append(it)
        elif kind != "commit":
            if any(_similar(words, w) for w in kept_words.get(kind, [])):
                drop[it["id"]] = "duplicate"
            else:
                kept_words.setdefault(kind, []).append(words)
    for it in events[:-MAX_EVENTS]:  # keep only the newest events
        drop[it["id"]] = "old event"
    for mid in drop:
        forget(mid)
    reasons: dict[str, int] = {}
    for r in drop.values():
        reasons[r] = reasons.get(r, 0) + 1
    return {"removed": len(drop), "kept": len(items) - len(drop), "reasons": reasons}
