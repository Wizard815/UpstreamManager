"""Sends Upstream Manager's days to the LLMWIKI vault's short-term memory.

The vault (an Obsidian vault behind the Local REST API plugin) keeps a long-term
wiki that only its Ingest workflow writes, and a short-term tier that tools like
this one feed. Through the vault's MCP endpoint and its memory tools:

- after the daily check, one line in today's daily note (memory_log), e.g.
  "Upkeep: omi-local fast-forwarded 12; argos request raised (2 conflicts)"
- once a finished day's repo logs are written, one new source for Ingest
  (memory_add_source), "raw/sources/<day> - Upstream Manager.md"

Off unless LLMWIKI_MCP_URL is set: on a family vault the bridge's
http://192.168.20.5:27123/family-mcp with a service token (writes go to the
shared household/ memory), on a single-user vault /mcp with the Local REST API
key. The key comes from LLMWIKI_API_KEY and is never stored. Everything runs in the
background and only logs a failure: the wiki being down never breaks the app.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading

log = logging.getLogger(__name__)

SOURCE = "upstream-manager"
TIMEOUT = 30


def enabled() -> bool:
    return bool(os.environ.get("LLMWIKI_MCP_URL", "").strip())


class WikiError(Exception):
    """The vault refused or failed a call; the message is its own."""


async def _call(name: str, arguments: dict) -> str:
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    url = os.environ["LLMWIKI_MCP_URL"].strip()
    key = os.environ.get("LLMWIKI_API_KEY", "").strip()
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    async with httpx2.AsyncClient(headers=headers, timeout=TIMEOUT) as http, \
            streamable_http_client(url, http_client=http) as streams:
        async with ClientSession(streams[0], streams[1]) as session:
            await session.initialize()
            result = await session.call_tool(name, arguments)
    text = "\n".join(getattr(c, "text", "") for c in result.content or [])
    if result.is_error:
        raise WikiError(text or f"{name} failed")
    return text


def call_tool(name: str, arguments: dict) -> str:
    """One tool call on the vault's MCP endpoint (blocking)."""
    return asyncio.run(asyncio.wait_for(_call(name, arguments), TIMEOUT * 2))


def _in_background(what: str, fn, *args) -> None:
    if not enabled():
        return

    def run():
        try:
            fn(*args)
        except Exception as e:  # the wiki is an extra: say why, carry on
            log.warning("LLMWIKI: %s failed: %s", what, e)

    threading.Thread(target=run, daemon=True, name=f"llmwiki-{what}").start()


def summarize_daily(results: list[dict]) -> str:
    """The daily check in one line, or "" when nothing happened worth a line."""
    merged = [f"{r.get('repo_name') or r['repo_id']} fast-forwarded {r.get('behind', 0)}"
              for r in results if r.get("status") == "merged"]
    raised = []
    for r in results:
        if r.get("status") == "request_raised":
            n = len(r.get("conflicts") or [])
            raised.append(f"{r.get('repo_name') or r['repo_id']} request raised" + (f" ({n} conflicts)" if n else ""))
    failed = [f"{r.get('repo_name') or r['repo_id']} failed" for r in results if r.get("status") == "error"]
    parts = merged + raised + failed
    return ("Upkeep: " + "; ".join(parts)) if parts else ""


def log_daily(results: list[dict]) -> None:
    """After the daily check: one line in today's daily note, if anything changed."""
    line = summarize_daily(results)
    if line:
        _in_background("daily line", call_tool, "memory_log", {"text": line, "source": SOURCE})


def day_source(day: str, logs: list[tuple[str, str]]) -> str:
    """Markdown for one finished day: each repo's day log under its name."""
    parts = [f"Upstream Manager's day logs for {day}, one per repository that had activity."]
    for name, text in logs:
        body = text.split("\n\n", 1)[1] if text.startswith("Log for ") and "\n\n" in text else text
        parts.append(f"## {name}\n\n{body.strip()}")
    return "\n\n".join(parts) + "\n"


def _send_day(day: str, logs: list[tuple[str, str]]) -> None:
    try:
        call_tool("memory_add_source", {"title": "Upstream Manager", "content": day_source(day, logs), "date": day})
    except WikiError as e:
        if "already exists" not in str(e):  # sent before: sources are never overwritten
            raise


def send_days(days: dict[str, list[tuple[str, str]]]) -> None:
    """Hand each finished day's logs to the vault once, for its Ingest workflow."""
    if not days or not enabled():
        return

    def run():
        for day in sorted(days):
            _send_day(day, days[day])

    _in_background("day logs", run)
