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
    saved = store.settings().get("wiki")
    s = dict(saved or {})
    # LLMWIKI_MCP_URL in .env connects the reading side too (over the Local REST API next to it),
    # unless the page was used to set reading up some other way
    from_env = saved is None and enabled()
    s.setdefault("enabled", from_env)
    s.setdefault("mode", "rest" if from_env else "folder")          # folder | rest
    s.setdefault("record_tasks", True)      # finished tasks go to the vault's short-term tier
    s.setdefault("priorities_note", "")     # the note the Overseer follows; "" = "Upstream priorities"
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


# ======================================================================
# Search and read the long-term notes (agents and chats), the priorities page the Overseer
# follows, and finished work handed to the vault's short-term tier.
# ======================================================================
def _within_wiki(rel: str) -> str:
    rel = (rel or "").strip().lstrip("/")
    if not rel.endswith(".md"):
        rel += ".md"
    if not rel.startswith("wiki/"):
        rel = "wiki/" + rel
    if ".." in rel.split("/"):
        raise WikiError("path outside wiki/")
    return rel


_STOP = {"the", "a", "an", "of", "for", "in", "on", "and", "or", "to", "is", "what", "how", "about", "my", "our", "with",
         "repo", "repository", "project", "wiki", "note", "notes", "please", "find", "look", "up", "do", "does", "we",
         "i", "you", "are", "it", "be", "can", "should", "when", "where", "which", "who", "why", "there", "this", "that"}


def _terms(query: str) -> list[str]:
    words = list(dict.fromkeys(re.findall(r"[\w./#-]{2,}", (query or "").lower())))
    return [w for w in words if w not in _STOP][:8] or words[:8]


def search(query: str, limit: int = 20) -> list[dict]:
    """Notes in wiki/ (the long-term tier) that mention every word of `query`, best first:
    [{note, path, status, score, lines}]. The vault's own search when reached over REST."""
    s = settings()
    if not s["enabled"]:
        raise WikiError("the LLM wiki is off (Settings → LLM wiki)")
    terms = _terms(query)
    if not terms:
        raise WikiError("search for at least one word")
    v = vault()
    candidates = None
    if isinstance(v, _Rest):   # let the vault narrow it down (Local REST API simple search), word by word
        found = []
        for t in terms:
            r = v._req("POST", "/search/simple/", params={"query": t, "contextLength": 80})
            if r.status_code >= 400:
                found = None
                break
            found += [h.get("filename") for h in r.json() if isinstance(h, dict) and str(h.get("filename", "")).startswith("wiki/")]
        candidates = list(dict.fromkeys(found)) if found is not None else None
    need = max(1, (len(terms) + 1) // 2)        # most of the words, not all: agents ask in sentences
    out = []
    for rel in candidates if candidates is not None else v.notes("wiki"):
        if "/logs/" in rel:
            continue
        text = v.read(rel) or ""
        meta, body = _front(text)
        title = rel.rsplit("/", 1)[-1][:-3]
        hay = (title + "\n" + body).lower()
        hits = [t for t in terms if t in hay]
        if len(hits) < need:
            continue
        lines = [l.strip() for l in body.splitlines() if not l.lstrip().startswith("#") and any(t in l.lower() for t in hits)][:6]
        score = 20 * len(hits) + sum(hay.count(t) for t in hits) + 10 * sum(t in title.lower() for t in hits)
        out.append({"note": title, "path": rel, "status": meta.get("status", ""), "score": score,
                    "lines": [redact(l)[:300] for l in lines]})
    out.sort(key=lambda n: -n["score"])
    return out[:max(1, min(int(limit), 50))]


def read_note(path: str, limit: int = 20000) -> dict:
    """One note of wiki/, secrets removed."""
    if not settings()["enabled"]:
        raise WikiError("the LLM wiki is off (Settings → LLM wiki)")
    rel = _within_wiki(path)
    text = vault().read(rel)
    if text is None:
        raise WikiError(f"no note {rel}")
    meta, body = _front(text)
    return {"path": rel, "note": rel.rsplit("/", 1)[-1][:-3], "status": meta.get("status", ""),
            "text": redact(body)[:limit], "truncated": len(body) > limit}


# ------------------------------------------------------------------ the priorities page
PRIORITIES_NOTE = "Upstream priorities"
_WEIGHTS = [(re.compile(r"\b(pause[sd]?|freeze|frozen|on hold|hold|skip|ignore|don'?t touch)\b", re.I), -100, "paused"),
            (re.compile(r"\b(urgent|critical|asap|right now|now|blocker|broken)\b", re.I), 60, "urgent"),
            (re.compile(r"\b(high|important|focus|priority|first|this week)\b", re.I), 30, "high"),
            (re.compile(r"\b(low|later|someday|nice to have|eventually)\b", re.I), -20, "low")]


def _weight(text: str) -> tuple[int, str] | None:
    for pat, w, word in _WEIGHTS:
        if pat.search(text):
            return w, word
    return None


def priorities(repos: list[dict] | None = None) -> dict:
    """What the user wrote in the wiki note "Upstream priorities" (settings: wiki.priorities_note), per repo:
    {note, path, repos: {repo_id: {weight, word, line}}, lines}. A line names repos by id or name, or a group
    as #group; its words say how much it matters (urgent / high / low / pause...), else the heading above
    the line does, else it counts as wanted (+20). Cached like the rest of the reading."""
    s = settings()
    empty = {"note": None, "path": None, "repos": {}, "lines": []}
    if not s["enabled"]:
        return empty
    want = (s.get("priorities_note") or PRIORITIES_NOTE).strip()
    key = "prio:" + want
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < 120:
        text, rel = hit[1]
    else:
        v = vault()
        rel = None
        try:
            direct = _within_wiki(want)
            if v.read(direct) is not None:
                rel = direct
        except WikiError:
            pass
        if not rel:
            rel = next((n for n in v.notes("wiki") if n.rsplit("/", 1)[-1][:-3].lower() == want.lower().rsplit("/", 1)[-1].removesuffix(".md")), None)
        text = (v.read(rel) or "") if rel else ""
        _CACHE[key] = (time.time(), (text, rel))
    if not rel:
        return empty
    repos = repos if repos is not None else store.snapshot(store.all_repos())
    names = {}
    for r in repos:
        for n in {r.get("id"), r.get("name")} - {None, ""}:
            names[n.lower()] = r["id"]
    groups = {}
    for r in repos:
        for g in r.get("groups") or []:
            groups.setdefault(g.lower(), []).append(r["id"])
    out, lines, heading = {}, [], None
    _, body = _front(text)
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") and not re.match(r"^#[\w-]+(\s|$)", line):   # a heading, not a #group
            heading = _weight(line)
            continue
        hit_ids = {rid for n, rid in names.items() if re.search(r"(?<![\w-])" + re.escape(n) + r"(?![\w-])", line, re.I)}
        for g in re.findall(r"#([\w-]+)", line):
            hit_ids.update(groups.get(g.lower(), []))
        if not hit_ids:
            continue
        w = _weight(line) or heading or (20, "wanted")
        clean = redact(line.lstrip("-*0123456789. "))[:200]
        lines.append({"text": clean, "weight": w[0], "word": w[1], "repos": sorted(hit_ids)})
        for rid in hit_ids:
            if rid not in out or abs(w[0]) > abs(out[rid]["weight"]):
                out[rid] = {"weight": w[0], "word": w[1], "line": clean}
    return {"note": rel.rsplit("/", 1)[-1][:-3], "path": rel, "repos": out, "lines": lines}


# ------------------------------------------------------------------ finished work -> the vault
def task_done_text(task: dict, repo: dict | None) -> tuple[str, str]:
    """(one line for the daily note, a note for the inbox) about a finished task."""
    who = (task.get("claimed_by") or {}).get("agent") or (task.get("where") or {}).get("agent")
    hands = sorted({c["by"] for c in task.get("comments") or [] if c.get("by") not in ("you", "overseer")})
    by = ", ".join(hands) or who or "you"
    repo_name = (repo or {}).get("name") or task["repo_id"]
    commits = [l["text"] for l in task.get("log") or [] if l["text"].startswith(("committed ", "its change went in"))]
    line = redact(f"Upkeep task done: {task['title']} ({repo_name}, {task['kind']}, by {by})")[:300]
    parts = [f"# Done: {task['title']}", "",
             f"- Repo: {repo_name}", f"- Kind: {task['kind']}, priority {task.get('priority', 'normal')}",
             f"- Done by: {by}", f"- Upstream Manager task {task['id']}, opened {task['created_at'][:10]}, done {config.now_iso()[:10]}"]
    parts += [f"- {c}" for c in commits[-4:]]
    if task.get("body"):
        parts += ["", "## What was asked", "", task["body"][:3000]]
    notes = [c for c in task.get("comments") or [] if c.get("by") != "overseer"][-6:]
    if notes:
        parts += ["", "## What happened (the task's thread)", ""] + [f"- {c['by']}: {c['text'][:800]}" for c in notes]
    return line, redact("\n".join(parts))[:12000]


def record_task_done(task: dict) -> None:
    """Hand a finished task to the vault: a line in today's daily note and a note for its inbox (Capture
    files it into the long-term tier). Through the MCP writer, else as an inbox note via the reader."""
    s = settings()
    if not (enabled() or s["enabled"]) or not s.get("record_tasks", True):
        return
    repo = store.get_repo(task["repo_id"])
    line, note = task_done_text(task, repo)

    def run():
        if enabled():
            call_tool("memory_log", {"text": line, "source": SOURCE})
            call_tool("memory_capture", {"title": f"Done: {task['title']}"[:120], "content": note})
            return
        from datetime import datetime
        v = vault()
        safe = re.sub(r"\s+", " ", re.sub(r'[\\/:*?"<>|#^\[\]]+', " ", task["title"])).strip()[:70] or "Task"
        v.write(f"inbox/{datetime.now().strftime('%Y-%m-%d %H%M')} - Done - {safe}.md", note)

    if enabled():
        _in_background("task done", run)
    else:
        threading.Thread(target=lambda: _quiet(run), daemon=True, name="llmwiki-task-done").start()


def _quiet(fn):
    try:
        fn()
    except Exception as e:
        log.warning("LLMWIKI: recording a finished task failed: %s", e)


from . import config  # noqa: E402  (after the module's own imports, like store above)
