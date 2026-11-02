"""Long-term repo memory in one SQLite file: no embedding model, no vector store.

``memory.db`` sits next to the state file and holds:

  * items     what the assistant remembers. Each belongs to one repo and has a
              kind: note (yours), chat (a durable fact from a chat), event
              (daily check, approvals), commit (indexed commits) or daylog (the
              model's log of one day's work).
  * links     the graph: each item is linked to the files and commit ids it
              mentions, so "what else touched src/app.py?" is an exact lookup,
              in this repo or any other.
  * messages  every chat message as it is sent, kept even after its chat
              session is deleted, so a day's work can always be logged.

Search is SQLite full-text search (keywords, with stemming) plus the links.

Daily logs: for each repo and each day with chat messages or events, the chat
model writes a log of what was worked on, decided and left open. Finished days
are logged in the background (hourly, and after the daily check); today's log
can be written on demand. Asking about "yesterday" or a date brings that day's
log into the chat.

Config (env wins over settings["memory"]):
  UPSTREAM_MEMORY=off            disable memory entirely
  UPSTREAM_MEMORY_AUTOCLEAN=off  no automatic cleanup
  UPSTREAM_DAILY_LOG=off         no automatic daily logs
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from . import config, store

log = logging.getLogger(__name__)

_lock = threading.RLock()
_db: sqlite3.Connection | None = None
_error: str | None = None

_COMMIT_FACT = re.compile(r"\b[0-9a-f]{8}\b")
_SHA = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b")
_FILE = re.compile(r"(?<![\w/.-])(?:[\w.-]+/)*[\w-][\w.-]*\.[A-Za-z][A-Za-z0-9]{0,7}\b|(?<![\w/.-])[\w.-]+/[\w./-]*[\w-]")
_WORD = re.compile(r"[a-z0-9_]+")
_STOP = set("""a an and are as at be but by can could did do does for from had has have how i if in into is it its
me my no not of on or our should so than that the their them then there these they this to was we were what when
where which who why will with would you your about any all just like make get got please tell show want need know
yes ok okay hi hello thanks""".split())

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS items (
  id INTEGER PRIMARY KEY, repo TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
  created_at TEXT NOT NULL, day TEXT NOT NULL, sha TEXT);
CREATE INDEX IF NOT EXISTS items_repo ON items(repo, kind);
CREATE INDEX IF NOT EXISTS items_day ON items(repo, day);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
  text, content='items', content_rowid='id', tokenize='porter unicode61');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text); END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
  DELETE FROM links WHERE item_id = old.id; END;
CREATE TABLE IF NOT EXISTS links (item_id INTEGER NOT NULL, ref TEXT NOT NULL, PRIMARY KEY (item_id, ref));
CREATE INDEX IF NOT EXISTS links_ref ON links(ref);
CREATE TABLE IF NOT EXISTS messages (
  repo TEXT NOT NULL, session TEXT NOT NULL, role TEXT NOT NULL, content TEXT NOT NULL,
  ts TEXT NOT NULL, day TEXT NOT NULL, PRIMARY KEY (repo, session, ts, role));
CREATE INDEX IF NOT EXISTS messages_day ON messages(repo, day);
"""


def _settings() -> dict:
    s = store.settings().get("memory", {})

    def on(env: str, key: str) -> bool:
        return (os.environ.get(env) or s.get(key, "on")) != "off"
    return {"enabled": on("UPSTREAM_MEMORY", "enabled"),
            "auto_cleanup": on("UPSTREAM_MEMORY_AUTOCLEAN", "auto_cleanup"),
            "daily_log": on("UPSTREAM_DAILY_LOG", "daily_log")}


def db_path() -> Path:
    return config.CONFIG_PATH.parent / "memory.db"


def get() -> sqlite3.Connection | None:
    """The database connection, or None if memory is off or failed to open."""
    global _db, _error
    if _db is not None or _error is not None:
        return _db
    # Read the state file before taking the memory lock: chat code holds the
    # store lock while it records messages here, so never wait for it in reverse.
    enabled = _settings()["enabled"]
    old = _old_messages() if enabled else []
    with _lock:
        if _db is None and _error is None:
            if not enabled:
                _error = "memory disabled"
            else:
                try:
                    path = db_path()
                    path.parent.mkdir(parents=True, exist_ok=True)
                    conn = sqlite3.connect(path, check_same_thread=False)
                    conn.row_factory = sqlite3.Row
                    conn.executescript(SCHEMA)
                    conn.executemany("INSERT OR IGNORE INTO messages(repo, session, role, content, ts, day) "
                                     "VALUES (?,?,?,?,?,?)", old)
                    conn.commit()
                    _db = conn
                except Exception as e:
                    _error = f"{type(e).__name__}: {e}"
                    log.warning("memory unavailable: %s", _error)
        return _db


def reload() -> dict:
    """Close the database so the next call reopens it with current settings."""
    global _db, _error
    with _lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:
                pass
        _db, _error = None, None
    get()
    return status()


def status() -> dict:
    out = {"ok": _db is not None, "error": _error, "store": "sqlite", "path": str(db_path()), **_settings()}
    if _db is not None:
        with _lock:
            out["memories"] = _db.execute("SELECT COUNT(*) FROM items").fetchone()[0]
            out["messages"] = _db.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    return out


# ------------------------------------------------------------------ helpers
def _day(ts: str | None = None) -> str:
    """Local calendar day of an ISO timestamp (or now): days follow the user's clock."""
    try:
        return datetime.fromisoformat(ts).astimezone().date().isoformat() if ts else date.today().isoformat()
    except ValueError:
        return (ts or "")[:10] or date.today().isoformat()


def refs(text: str) -> set[str]:
    """Commit ids and file paths a text mentions (lower-case; files also by bare name)."""
    out = {m.group(0) for m in _SHA.finditer(text.lower())}
    for m in _FILE.finditer(text):
        path = m.group(0).strip("./").lower()
        if len(path) >= 3 and not path.startswith("http") and not re.fullmatch(r"[\d.]+", path):
            out.add(path)
            out.add(path.rsplit("/", 1)[-1])
    return out


def _terms(query: str) -> list[str]:
    seen: list[str] = []
    for w in _WORD.findall(query.lower()):
        if (len(w) >= 3 or w.isdigit()) and w not in _STOP and w not in seen:
            seen.append(w)
    return seen[:24]


def _stem(w: str) -> str:
    return w[:5] if len(w) > 5 else w


def _fmt(row: sqlite3.Row, score: float | None = None) -> dict:
    return {"id": str(row["id"]), "memory": row["text"], "repo": row["repo"], "kind": row["kind"],
            "day": row["day"], "score": score, "created_at": row["created_at"]}


# ------------------------------------------------------------------ writing
def remember(repo_id: str, text: str, kind: str, extra: dict | None = None,
             day: str | None = None, link: set[str] | None = None) -> list[dict]:
    """Store `text` as a memory of `repo_id` (no model call). Exact repeats are not stored twice."""
    db = get()
    text = (text or "").strip()
    if db is None or not text:
        return []
    now = config.now_iso()
    with _lock:
        row = db.execute("SELECT * FROM items WHERE repo=? AND kind=? AND text=?", (repo_id, kind, text)).fetchone()
        if row:
            return [_fmt(row)]
        cur = db.execute("INSERT INTO items(repo, kind, text, created_at, day, sha) VALUES (?,?,?,?,?,?)",
                         (repo_id, kind, text, now, day or _day(now), (extra or {}).get("sha")))
        item_id = cur.lastrowid
        db.executemany("INSERT OR IGNORE INTO links(item_id, ref) VALUES (?,?)",
                       [(item_id, r) for r in refs(text) | (link or set())])
        db.commit()
        return [_fmt(db.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone())]


def record_message(repo_id: str, session_id: str, role: str, content: str, ts: str) -> None:
    """Keep a chat message for day logs and recall (survives deleting its session)."""
    db = get()
    if db is None or not content:
        return
    with _lock:
        db.execute("INSERT OR IGNORE INTO messages(repo, session, role, content, ts, day) VALUES (?,?,?,?,?,?)",
                   (repo_id, session_id, role, content, ts, _day(ts)))
        db.commit()


def _old_messages() -> list[tuple]:
    """Chat messages already in the state file (kept before this database existed)."""
    rows = []
    for repo in store.snapshot(store.all_repos()):
        for s in repo.get("chat_sessions") or []:
            for m in s.get("messages") or []:
                if m.get("timestamp") and not m.get("error") and m.get("content"):
                    rows.append((repo["id"], s["id"], m["role"], m["content"], m["timestamp"], _day(m["timestamp"])))
    return rows


def forget(memory_id: str) -> None:
    db = get()
    if db is not None:
        with _lock:
            db.execute("DELETE FROM items WHERE id=?", (int(memory_id),))
            db.commit()


# ------------------------------------------------------------------ reading
def search(query: str, repo_id: str | None = None, exclude: str | None = None, k: int = 8,
           kind: str | None = None, exclude_kind: str | None = None) -> list[dict]:
    """Memories matching `query`, best first.

    score is 1.0 for an item linked to a file or commit the query names, else the
    share of the query's words the item contains (0..1).
    """
    db = get()
    if db is None or not query.strip():
        return []
    where, args = [], []
    if repo_id:
        where.append("i.repo = ?"); args.append(repo_id)
    elif exclude:
        where.append("i.repo != ?"); args.append(exclude)
    if kind:
        where.append("i.kind = ?"); args.append(kind)
    elif exclude_kind:
        where.append("i.kind != ?"); args.append(exclude_kind)
    cond = (" AND " + " AND ".join(where)) if where else ""
    terms, wanted = _terms(query), refs(query)
    found: dict[int, tuple[float, float, sqlite3.Row]] = {}
    with _lock:
        if wanted:
            marks = ",".join("?" * len(wanted))
            for row in db.execute(f"SELECT DISTINCT i.* FROM items i JOIN links l ON l.item_id = i.id "
                                  f"WHERE l.ref IN ({marks}){cond} ORDER BY i.id DESC LIMIT ?",
                                  [*wanted, *args, k * 4]):
                found[row["id"]] = (1.0, 0.0, row)
        if terms:
            fts = " OR ".join(f'"{t}"' for t in terms)
            stems = {_stem(t) for t in terms}
            for row in db.execute(f"SELECT i.*, bm25(items_fts) AS rank FROM items_fts JOIN items i ON i.id = items_fts.rowid "
                                  f"WHERE items_fts MATCH ?{cond} ORDER BY rank LIMIT ?", [fts, *args, k * 6]):
                if row["id"] in found:
                    continue
                have = {_stem(w) for w in _WORD.findall(row["text"].lower())}
                found[row["id"]] = (round(len(stems & have) / len(stems), 2), row["rank"], row)
    best = sorted(found.values(), key=lambda t: (-t[0], t[1]))[:k]
    return [_fmt(row, score) for score, _, row in best]


def list_repo(repo_id: str, k: int = 200, kind: str | None = None) -> list[dict]:
    db = get()
    if db is None:
        return []
    sql, args = "SELECT * FROM items WHERE repo=?", [repo_id]
    if kind:
        sql += " AND kind=?"; args.append(kind)
    with _lock:
        rows = db.execute(sql + " ORDER BY created_at DESC, id DESC LIMIT ?", [*args, k]).fetchall()
    return [_fmt(r) for r in rows]


_DAY_WORDS = re.compile(r"\b(today|yesterday|(\d+) days? ago|(\d{4}-\d{2}-\d{2}))\b", re.I)


def days_mentioned(message: str) -> list[str]:
    """Days a message asks about: today, yesterday, "3 days ago", 2026-11-01."""
    today, out = date.today(), []
    for m in _DAY_WORDS.finditer(message):
        word = m.group(1).lower()
        if m.group(3):
            day = m.group(3)
        elif word == "today":
            day = today.isoformat()
        elif word == "yesterday":
            day = (today - timedelta(days=1)).isoformat()
        else:
            day = (today - timedelta(days=int(m.group(2)))).isoformat()
        if day not in out:
            out.append(day)
    return out[:3]


def day_logs(repo_id: str, days: list[str]) -> list[dict]:
    db = get()
    if db is None or not days:
        return []
    with _lock:
        rows = db.execute(f"SELECT * FROM items WHERE repo=? AND kind='daylog' AND day IN ({','.join('?' * len(days))}) "
                          "ORDER BY day", [repo_id, *days]).fetchall()
    return [_fmt(r, 1.0) for r in rows]


# ------------------------------------------------------------- model calls
def _complete(system: str, user: str, max_tokens: int, temperature: float = 0.1) -> str:
    """One chat-completion call to the app's own model connection."""
    import requests
    from . import llm
    cfg = llm.config()
    if not cfg.get("base_url") or not cfg.get("model"):
        raise RuntimeError("no model connection set (LLM Settings)")
    resp = requests.post(
        f"{cfg['base_url']}/chat/completions", headers=llm._headers(cfg["api_key"]), timeout=llm.TIMEOUT,
        json={"model": cfg["model"], "temperature": temperature, "max_tokens": max_tokens,
              "chat_template_kwargs": {"enable_thinking": False},
              "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
    )
    resp.raise_for_status()
    return (resp.json()["choices"][0]["message"].get("content") or "").strip()


EXTRACT_PROMPT = """You pick out long-term memories from a developer's chat message about a git repository.
Keep only durable facts about the user: preferences, rules, decisions, plans and constraints.
Ignore questions, greetings, and anything about specific commits, files or counts (the app tracks those itself).
Write each fact as one short sentence starting with "User".
Reply with JSON only: {"facts": ["..."]}, or {"facts": []} when there is nothing durable."""


def _extract_facts(user_msg: str) -> list[str]:
    """Ask the chat model for durable facts in one message."""
    text = _complete(EXTRACT_PROMPT, user_msg, 400)
    found = re.search(r"\{.*\}", text, re.S)
    facts = json.loads(found.group(0)).get("facts", []) if found else []
    return [f.strip() for f in facts if isinstance(f, str) and len(f.strip()) >= 8][:5]


def learn_from_chat(repo_id: str, user_msg: str, reply: str) -> None:
    """Pull durable facts out of the user's message, in the background.

    Only the user's message is used: replies restate repo facts the commit index
    already holds. Facts that still mention a commit id are dropped for the same reason.
    """
    def run():
        if get() is None:
            return
        try:
            facts = _extract_facts(user_msg)
        except Exception as e:
            log.warning("memory extraction failed for %s: %s", repo_id, e)
            return
        for fact in facts:
            if not _COMMIT_FACT.search(fact):
                remember(repo_id, fact, kind="chat")
        if facts and _settings()["auto_cleanup"]:
            cleanup(repo_id)
    threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------- daily logs
DAYLOG_PROMPT = """You keep a developer's work log for one git repository.
You get everything from one day: the chat conversations about the repo and the app's events
(daily checks, merges, approvals, notes). Write the log for that day in Markdown:

**Worked on** - what was looked at, changed or asked about
**Decided** - decisions, preferences and rules the user stated
**Problems** - errors, conflicts or failures and how they ended
**Open** - questions left unanswered and next steps

Keep file names, commit ids, branch and upstream names exactly as written so they can be
looked up later. Leave out a section that has nothing in it. Be factual and brief; never
invent anything that is not in the material."""
DAYLOG_INPUT_CHARS = 30000
DAYLOG_MESSAGE_CHARS = 1500


def _day_material(repo_id: str, day: str) -> tuple[str, int]:
    """The day's chats and events as one text for the model, and how many things it holds."""
    db = get()
    with _lock:
        msgs = db.execute("SELECT session, role, content, ts FROM messages WHERE repo=? AND day=? ORDER BY ts",
                          (repo_id, day)).fetchall()
        events = db.execute("SELECT kind, text, created_at FROM items WHERE repo=? AND day=? AND kind NOT IN ('daylog','commit') "
                            "ORDER BY created_at", (repo_id, day)).fetchall()
    titles = {s["id"]: s.get("title") for s in (store.get_repo(repo_id) or {}).get("chat_sessions") or []}
    parts, session = [], None
    for m in msgs:
        if m["session"] != session:
            session = m["session"]
            parts.append(f"\n## Chat: {titles.get(session) or session}")
        body = m["content"]
        if len(body) > DAYLOG_MESSAGE_CHARS:
            body = body[:DAYLOG_MESSAGE_CHARS] + " ...(cut)"
        parts.append(f"[{m['ts'][11:16]}] {m['role']}: {body}")
    if events:
        parts.append("\n## Events and notes")
        parts += [f"- ({e['kind']}) {e['text']}" for e in events]
    text = "\n".join(parts).strip()
    if len(text) > DAYLOG_INPUT_CHARS:  # keep the start and the end of a very long day
        half = DAYLOG_INPUT_CHARS // 2
        text = text[:half] + "\n\n...(middle of the day left out)...\n\n" + text[-half:]
    return text, len(msgs) + len(events)


def write_day_log(repo_id: str, day: str | None = None) -> dict:
    """Have the model write (or rewrite) the log of `day` (default today) for one repo."""
    db = get()
    if db is None:
        return {"ok": False, "error": _error or "memory is off"}
    day = day or date.today().isoformat()
    material, count = _day_material(repo_id, day)
    if not count:
        return {"ok": False, "error": f"nothing happened in this repo on {day}"}
    repo = store.get_repo(repo_id) or {}
    try:
        summary = _complete(DAYLOG_PROMPT, f"Repository: {repo.get('name') or repo_id}\nDay: {day}\n\n{material}", 1200, 0.2)
    except Exception as e:
        return {"ok": False, "error": f"model call failed: {e}"}
    if not summary:
        return {"ok": False, "error": "the model returned an empty log"}
    with _lock:
        db.execute("DELETE FROM items WHERE repo=? AND kind='daylog' AND day=?", (repo_id, day))
        db.commit()
    # Link the log to everything the day's material mentions, not only what the summary kept.
    item = remember(repo_id, f"Log for {day}:\n\n{summary}", kind="daylog", day=day, link=refs(material))
    return {"ok": True, "day": day, "sources": count, "item": item[0] if item else None}


_due_lock = threading.Lock()


def write_due_logs() -> list[dict]:
    """Log every finished day (before today) that had activity and has no log yet."""
    db = get()
    if db is None or not _settings()["daily_log"] or not _due_lock.acquire(blocking=False):
        return []
    try:
        today = date.today().isoformat()
        with _lock:
            due = db.execute("""
                SELECT a.repo, a.day FROM (SELECT repo, day FROM messages UNION
                                           SELECT repo, day FROM items WHERE kind IN ('event','note','chat','decision')) a
                WHERE a.day < ? AND NOT EXISTS (
                    SELECT 1 FROM items d WHERE d.kind='daylog' AND d.repo=a.repo AND d.day=a.day)
                ORDER BY a.day""", (today,)).fetchall()
        known = {r["id"] for r in store.all_repos()}
        done = []
        for row in due:
            if row["repo"] not in known:
                continue
            res = write_day_log(row["repo"], row["day"])
            done.append({"repo": row["repo"], "day": row["day"], **{k: res.get(k) for k in ("ok", "error")}})
            if not res["ok"] and res.get("error", "").startswith("model call failed"):
                break  # model is down: try again on the next pass
        return done
    finally:
        _due_lock.release()


def write_due_logs_soon() -> None:
    threading.Thread(target=write_due_logs, daemon=True).start()


_loop_started = False


def start_background(interval: int = 3600) -> None:
    """Write due day logs now and then every `interval` seconds (once per process)."""
    global _loop_started
    if _loop_started:
        return
    _loop_started = True

    def loop():
        stop = threading.Event()
        while True:
            try:
                write_due_logs()
            except Exception as e:
                log.warning("daily log pass failed: %s", e)
            stop.wait(interval)
    threading.Thread(target=loop, name="daily-logs", daemon=True).start()


# ------------------------------------------------------------------ cleanup
_COMMIT_SHA = re.compile(r"^Commit ([0-9a-f]{7,40}) ")
MAX_EVENTS = 50


def _similar(a: set, b: set) -> bool:
    return bool(a and b) and len(a & b) / len(a | b) >= 0.8


def cleanup(repo_id: str, dry_run: bool = False, thorough: bool = False, only_ids: list[str] | None = None) -> dict:
    """Prune a repo's memories: junk, duplicates and stale events. Day logs are never pruned.

    The automatic pass (after chat extraction and commit indexing) only removes
    things that are certainly redundant: empty items, repeated commits, facts that
    restate a commit, and events beyond the newest 50. `thorough` also removes
    near-duplicate wording; it is only used from the Clean up button, which
    previews first (dry_run) and then deletes exactly the previewed ids (only_ids).
    The oldest copy of a memory is the one kept.
    """
    if get() is None:
        return {"removed": 0, "kept": 0, "reasons": {}, "items": []}
    items = [i for i in reversed(list_repo(repo_id, k=5000)) if i["kind"] != "daylog"]  # oldest first
    drop: dict[str, str] = {}
    seen_sha: set[str] = set()
    seen_text: set[tuple[str, str]] = set()
    kept_words: dict[str, list[set]] = {}
    events: list[dict] = []
    for it in items:
        text = (it.get("memory") or "").strip()
        kind = it.get("kind") or "chat"
        words = set(_WORD.findall(text.lower()))
        key = (kind, " ".join(sorted(words)))
        if len(text) < 8:
            drop[it["id"]] = "empty"
        elif kind == "chat" and _COMMIT_FACT.search(text):
            drop[it["id"]] = "restated commit"
        elif kind == "commit" and (m := _COMMIT_SHA.match(text)):
            if m.group(1) in seen_sha:
                drop[it["id"]] = "duplicate commit"
            seen_sha.add(m.group(1))
        elif key in seen_text:
            drop[it["id"]] = "duplicate"
        elif kind == "event":
            events.append(it)
        elif thorough and kind != "commit" and any(_similar(words, w) for w in kept_words.get(kind, [])):
            drop[it["id"]] = "near-duplicate"
        else:
            kept_words.setdefault(kind, []).append(words)
        seen_text.add(key)
    for it in events[:-MAX_EVENTS]:  # keep only the newest events
        drop[it["id"]] = "old event"
    if only_ids is not None:
        wanted = {str(i) for i in only_ids}
        drop = {i: r for i, r in drop.items() if i in wanted}
    by_id = {it["id"]: it for it in items}
    preview = [{"id": i, "memory": by_id[i]["memory"], "kind": by_id[i]["kind"], "reason": r} for i, r in drop.items()]
    if not dry_run:
        for mid in drop:
            forget(mid)
    reasons: dict[str, int] = {}
    for r in drop.values():
        reasons[r] = reasons.get(r, 0) + 1
    return {"removed": 0 if dry_run else len(drop), "would_remove": len(drop), "kept": len(items) - len(drop),
            "reasons": reasons, "items": preview}
