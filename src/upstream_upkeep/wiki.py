"""The LLM wiki (an Obsidian vault kept by agents, like LLMWIKI): read it, and feed its daily note.

The vault keeps two tiers. ``wiki/`` is long-term, curated memory: Upstream
Manager only reads it, to give the chat and coding agents what the user wrote
down about a repo (where it runs, why it is a fork, ...), always with the note
it came from. ``daily/`` is short-term memory: Upstream Manager writes there,
and only there, into its own section of today's note (merges, requests,
what agents did, the day logs). The vault's own Rollover/Ingest workflow
decides what of that becomes long-term. Upstream Manager never writes to
``wiki/`` or ``raw/``, never edits a rolled-over daily note, and redacts
anything that looks like a secret before writing.

Two ways to reach the vault:
  * folder: a path to the vault on disk (a clone, a mounted share);
  * rest:   the Obsidian Local REST API plugin (http://host:27123), with its API
            key kept like the other accounts (keychain, or UPSTREAM_WIKI_API_KEY).
"""
from __future__ import annotations

import re
import time
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

from . import credentials, memory, store

SECTION = "## Upstream Manager"
_SECRET = re.compile(
    r"(gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{16,}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,}"            # JWTs
    r"|(?i:bearer)\s+[A-Za-z0-9._~+/=-]{16,}"
    r"|(?i:(?:password|passwd|secret|token|api[_-]?key)\s*[=:]\s*)\S{6,})")
_CACHE: dict[str, tuple[float, object]] = {}
CACHE_SECONDS = 600


class WikiError(RuntimeError):
    pass


def settings() -> dict:
    s = dict(store.settings().get("wiki") or {})
    s.setdefault("enabled", False)
    s.setdefault("mode", "folder")          # folder | rest
    s.setdefault("path", "")
    s.setdefault("url", "")
    s.setdefault("publish", True)           # write to today's daily note
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
        return _Rest(s["url"], credentials.get("wiki_api_key"))
    if not s["path"]:
        raise WikiError("set the vault's folder (Settings → LLM wiki)")
    return _Folder(s["path"])


def status() -> dict:
    s = settings()
    out = {**s, "ok": False, "error": None, "key_set": bool(credentials.get("wiki_api_key"))}
    if not s["enabled"]:
        return out
    try:
        v = vault()
        out["notes"] = len(v.notes("wiki"))
        out["today"] = _daily_path()
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


# ------------------------------------------------------------------ writing daily/
def _daily_path(day: str | None = None) -> str:
    return f"daily/{day or date.today().isoformat()}.md"


def _template(v, day: str) -> str:
    t = None
    try:
        t = v.read("schema/templates/DAILY.md")
    except WikiError:
        pass
    t = t or ("---\ntitle: \"{{date}}\"\ntype: daily\ntags: [daily]\ncreated: {{date}}\nstatus: open\nrelated: []\n---\n\n"
              "# {{date}}\n\n## Worked on\n\n- \n")
    return t.replace("{{date}}", day)


def _section(day: str) -> str:
    """Today's Upstream Manager material: day logs where written, else the day's events and agent notes."""
    db = memory.get()
    lines = []
    for repo in store.snapshot(store.all_repos()) + [{"id": store.GENERAL, "name": "General chats"}]:
        logs = memory.day_logs(repo["id"], [day]) if db is not None else []
        name = repo.get("name") or repo["id"]
        if logs:
            body = logs[0]["memory"].split("\n", 2)[-1].strip()
            lines.append(f"- **{name}**: " + " ".join(l.strip() for l in body.splitlines() if l.strip())[:900])
            continue
        if db is None:
            continue
        with memory._lock:
            rows = db.execute("SELECT kind, text FROM items WHERE repo=? AND day=? AND kind IN ('event','decision','note') "
                              "ORDER BY created_at", (repo["id"], day)).fetchall()
            agent = db.execute("SELECT content FROM messages WHERE repo=? AND day=? AND role='agent' ORDER BY ts",
                               (repo["id"], day)).fetchall()
        items = [re.sub(r"^\d{4}-\d{2}-\d{2}: ", "", r["text"]) for r in rows] + [f"agent: {a['content']}" for a in agent]
        if items:
            lines.append(f"- **{name}**: " + "; ".join(i.replace("\n", " ")[:300] for i in items[:8]))
    pending = [r for r in store.get_requests() if r.get("status") == "pending"]
    if pending:
        lines.append("- Waiting for approval: " + "; ".join(f"{r.get('repo_name') or r.get('repo_id')} ({r.get('type') or 'merge'})"
                                                           for r in pending[:10]))
    return "\n".join(lines)


def publish(day: str | None = None) -> dict:
    """Put Upstream Manager's section into the daily note (creating today's from the template).

    Only daily/; only a note that is still open; only our own section (rewritten
    each time, so publishing twice is harmless). Returns what was written.
    """
    s = settings()
    if not s["publish"]:
        return {"ok": False, "error": "publishing to the wiki is off"}
    day = day or date.today().isoformat()
    v = vault()
    rel = _daily_path(day)
    text = v.read(rel)
    if text is None:
        if day != date.today().isoformat():
            return {"ok": False, "error": f"there is no daily note for {day} (only today's is created)"}
        text = _template(v, day)
    meta, _ = _front(text)
    if meta.get("status", "open") != "open":
        return {"ok": False, "error": f"{rel} is {meta.get('status')}: rolled-over notes are never edited"}
    body = _section(day)
    if not body:
        return {"ok": True, "path": rel, "written": False, "detail": "nothing from Upstream Manager today"}
    block = (f"{SECTION}\n\n> [!info] Written by Upstream Manager (rewritten on each publish; edit above or below it).\n\n"
             f"{redact(body)}\n")
    pat = re.compile(rf"^{re.escape(SECTION)}\n.*?(?=^## |\Z)", re.S | re.M)
    new = pat.sub(lambda _: block + "\n", text, count=1) if pat.search(text) else text.rstrip("\n") + "\n\n" + block
    v.write(rel, new)
    return {"ok": True, "path": rel, "written": True, "lines": body.count("\n") + 1}


def publish_quietly() -> None:
    """After a day log or daily check: publish if the wiki is set up; never fail the caller."""
    try:
        s = settings()
        if s["enabled"] and s["publish"]:
            publish()
    except Exception:
        pass
