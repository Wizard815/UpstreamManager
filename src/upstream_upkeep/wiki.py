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


# ======================================================================
# Reading: what the vault's long-term notes (wiki/) say about a repo.
#
# Chats, agent briefings and the Memory tab get it, citing the note and
# marking drafts. Only wiki/ is read: daily/, inbox/ and raw/ are the
# short-term tier and not something to cite. Reached as a folder (a clone or
# mounted share) or through the Local REST API, whose key and address default
# to the ones the writer above uses (LLMWIKI_API_KEY, LLMWIKI_MCP_URL).
# Nothing here writes to the vault.
# ======================================================================
import re
import time
from pathlib import Path
from urllib.parse import quote

import requests

from . import credentials, store


def rest_key() -> str:
    return credentials.get("wiki_api_key") or os.environ.get("LLMWIKI_API_KEY", "").strip()


def rest_url() -> str:
    """The Local REST API: the saved address, else the writer's MCP address without /mcp."""
    saved = (store.settings().get("wiki") or {}).get("url") or ""
    mcp = os.environ.get("LLMWIKI_MCP_URL", "").strip()
    return (saved or re.sub(r"/mcp/?$", "", mcp)).rstrip("/")


_SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"            # JWTs
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S{6,})")
_CACHE: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 600


def settings() -> dict:
    s = dict(store.settings().get("wiki") or {})
    s.setdefault("enabled", False)
    s.setdefault("mode", "folder")          # folder | rest
    s.setdefault("path", "")
    s.setdefault("url", "")
    s.setdefault("read", True)              # give chats and agents what wiki/ says about a repo
    return s


def redact(text: str) -> str:
    """Never let a secret reach the vault (its rule 9)."""
    return _SECRET.sub("[redacted]", text)


# ------------------------------------------------------------------ vault access
class _Folder:
    def __init__(self, root: str):
        self.root = Path(root).expanduser()
        if not (self.root / "wiki").is_dir() and not (self.root / ".obsidian").is_dir():
            raise WikiError(f"{self.root} doesn't look like a vault (no wiki/ or .obsidian/)")

    def _p(self, rel: str) -> Path:
        p = (self.root / rel).resolve()
        if self.root.resolve() not in p.parents:
            raise WikiError("path outside the vault")
        return p

    def read(self, rel: str) -> str | None:
        p = self._p(rel)
        return p.read_text(encoding="utf-8") if p.is_file() else None

    def write(self, rel: str, text: str) -> None:
        p = self._p(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def notes(self, folder: str) -> list[str]:
        base = self._p(folder)
        return sorted(str(p.relative_to(self.root)) for p in base.rglob("*.md")) if base.is_dir() else []


class _Rest:
    def __init__(self, url: str, key: str):
        if not url:
            raise WikiError("set the vault's Local REST API URL")
        self.url, self.key = url.rstrip("/"), key

    def _req(self, method, path, **kw):
        h = {"Authorization": f"Bearer {self.key}"} if self.key else {}
        h.update(kw.pop("headers", {}))
        try:
            r = requests.request(method, self.url + path, headers=h, timeout=20, **kw)
        except requests.RequestException as e:
            raise WikiError(f"can't reach the vault at {self.url}: {type(e).__name__}")
        if r.status_code == 401:
            raise WikiError("the vault refused the API key (Settings → LLM wiki)")
        return r

    def read(self, rel: str) -> str | None:
        r = self._req("GET", "/vault/" + quote(rel), headers={"Accept": "text/markdown"})
        if r.status_code == 404:
            return None
        if r.status_code >= 400:
            raise WikiError(f"vault read failed: HTTP {r.status_code}")
        return r.text

    def write(self, rel: str, text: str) -> None:
        r = self._req("PUT", "/vault/" + quote(rel), data=text.encode("utf-8"), headers={"Content-Type": "text/markdown"})
        if r.status_code >= 400:
            raise WikiError(f"vault write failed: HTTP {r.status_code}")

    def notes(self, folder: str) -> list[str]:
        out, todo = [], [folder.rstrip("/") + "/"]
        while todo and len(out) < 2000:
            d = todo.pop()
            r = self._req("GET", "/vault/" + quote(d), headers={"Accept": "application/json"})
            if r.status_code >= 400:
                continue
            for f in r.json().get("files", []):
                (todo if f.endswith("/") else out).append(d + f)
        return sorted(f for f in out if f.endswith(".md"))


def vault():
    s = settings()
    if not s["enabled"]:
        raise WikiError("the LLM wiki is off (Settings → LLM wiki)")
    if s["mode"] == "rest":
        return _Rest(rest_url(), rest_key())
    if not s["path"]:
        raise WikiError("set the vault's folder (Settings → LLM wiki)")
    return _Folder(s["path"])


def status() -> dict:
    s = settings()
    out = {**s, "ok": False, "error": None, "key_set": bool(rest_key()), "rest_url": rest_url(),
           "writer": enabled(), "writer_url": os.environ.get("LLMWIKI_MCP_URL", "").strip() or None}
    if not s["enabled"]:
        return out
    try:
        v = vault()
        out["notes"] = len(v.notes("wiki"))
        out["ok"] = True
    except WikiError as e:
        out["error"] = str(e)
    return out


# ------------------------------------------------------------------ reading wiki/
def _front(text: str) -> tuple[dict, str]:
    m = re.match(r"^---\n(.*?)\n---\n?", text, re.S)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith(" "):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip('"')
    return meta, text[m.end():]


def about(repo: dict, limit: int = 3500) -> list[dict]:
    """What the long-term tier (wiki/ only) says about a repo: [{note, status, lines}], cached."""
    s = settings()
    if not (s["enabled"] and s["read"]):
        return []
    names = {n.lower() for n in (repo.get("name"), repo.get("id")) if n}
    key = "about:" + "|".join(sorted(names))
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_SECONDS:
        return hit[1]
    v = vault()
    pat = re.compile(r"(?<![\w-])(" + "|".join(re.escape(n) for n in names) + r")(?![\w-])", re.I)
    found, used = [], 0
    for rel in v.notes("wiki"):
        if "/logs/" in rel:
            continue  # agent activity logs, not knowledge
        title = rel.rsplit("/", 1)[-1][:-3]
        text = v.read(rel) or ""
        meta, body = _front(text)
        lines = [l.strip() for l in body.splitlines() if pat.search(l)]
        if title.lower() in names:  # a note of its own: its opening lines
            lines = [l.strip() for l in body.splitlines() if l.strip() and not l.startswith("```")][:12]
        if not lines:
            continue
        chunk = [redact(l)[:400] for l in lines[:8]]
        used += sum(len(l) for l in chunk)
        found.append({"note": title, "path": rel, "status": meta.get("status", ""), "lines": chunk})
        if used > limit:
            break
    found.sort(key=lambda f: (f["note"].lower() not in names, f["note"]))
    _CACHE[key] = (time.time(), found)
    return found


def about_text(repo: dict) -> str:
    try:
        notes = about(repo)
    except WikiError as e:
        return f"(LLM wiki unavailable: {e})"
    if not notes:
        return ""
    out = ["From the user's LLM wiki (long-term notes; cite the note, and treat status: draft as unverified):"]
    for n in notes:
        out.append(f"[[{n['note']}]]" + (" (draft)" if n["status"] == "draft" else "") + ":")
        out += [f"  {l}" for l in n["lines"]]
    return "\n".join(out)


# ------------------------------------------------------------------ parking a task
def park(title: str, content: str) -> dict:
    """Park something for later in the vault's inbox (its short-term tier; Capture files it later).

    Uses the vault's memory_capture tool when the MCP writer is set (LLMWIKI_MCP_URL), else writes
    inbox/YYYY-MM-DD HH:MM - <title>.md the same way through the reader's folder or REST access.
    Never anything under wiki/, and never a secret."""
    from datetime import datetime
    title, content = redact(title).strip()[:120] or "Task", redact(content)
    if enabled():
        res = call_tool("memory_capture", {"title": title, "content": content})
        return {"via": "mcp", "note": res.strip()[:300]}
    s = settings()
    if not s["enabled"]:
        raise WikiError("the LLM wiki isn't set up: set LLMWIKI_MCP_URL, or a vault folder / REST API in Settings")
    v = vault()
    safe = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", title)).strip()[:80] or "Task"
    stamp = datetime.now().strftime("%Y-%m-%d %H%M")
    rel = f"inbox/{stamp} - {safe}.md"
    n = 2
    while v.read(rel) is not None:
        rel = f"inbox/{stamp} - {safe} ({n}).md"
        n += 1
    v.write(rel, content)
    return {"via": s["mode"], "note": rel}
