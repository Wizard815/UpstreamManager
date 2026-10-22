"""File edits proposed by the chat model, applied only when the user says so.

The model writes edits as search/replace blocks (small local models handle
these far better than unified diffs):

    EDIT path/inside/the/repo
    <<<<<<< SEARCH
    exact lines from the current file
    =======
    the lines that replace them
    >>>>>>> REPLACE

An empty SEARCH part creates a new file (refused if it exists). Nothing is
written until the user presses Apply. Each apply records the previous
content so Undo restores it exactly. Edits are never committed: the user
reviews them in git like any other working-tree change.
"""
from __future__ import annotations

import difflib
import re
import uuid
from pathlib import Path

MAX_FILE = 400_000
CRLF, LF = "\r\n", "\n"
_BLOCK = re.compile(
    r"EDIT[ \t]+`?(?P<path>[^\n`]+?)`?[ \t]*\n(?:```[\w-]*\n)?<{5,9} ?SEARCH[ \t]*\n(?P<search>.*?)\n?={5,9}[ \t]*\n(?P<replace>.*?)\n?>{5,9} ?REPLACE",
    re.S,
)

EDIT_INSTRUCTIONS = """Making changes: when the user asks you to change files, do not describe the change; write one block per change in exactly this form (no diff format, no line numbers):
EDIT path/inside/the/repo
<<<<<<< SEARCH
lines copied exactly from the current file, enough to be unique
=======
the new lines
>>>>>>> REPLACE
Only edit files whose contents were attached in this conversation, and copy the SEARCH lines exactly from them. Leave SEARCH empty to create a new file. Keep each block small. The user reviews and applies the edits; you cannot apply them yourself."""


def parse(text: str) -> list[dict]:
    """Edit blocks found in a model reply."""
    return [{"path": _clean_path(m["path"]), "search": m["search"], "replace": m["replace"]}
            for m in _BLOCK.finditer(text or "")]


def _clean_path(raw: str) -> str:
    """Normalise a path the model wrote; keeps dot-directories like .github intact."""
    p = raw.strip().replace("\\", "/")
    while p.startswith("./") or p.startswith("/"):
        p = p[2:] if p.startswith("./") else p[1:]
    return p


def _read(p: Path) -> str:
    # newline="" keeps line endings exactly as they are (no CRLF<->LF churn on Windows)
    with open(p, encoding="utf-8", newline="") as f:
        return f.read()


def _write(p: Path, text: str) -> None:
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _target(root: Path, rel: str) -> Path | None:
    p = (root / rel).resolve()
    if root not in p.parents or ".git" in p.relative_to(root).parts:
        return None
    return p


def _locate(text: str, search: str) -> tuple[int, int] | None:
    """Span of `search` in `text`: exact first, then ignoring line endings and trailing spaces."""
    i = text.find(search)
    if i >= 0 and text.find(search, i + 1) < 0:
        return i, i + len(search)
    if i >= 0:
        return None  # ambiguous: matches more than once
    raw = text.splitlines(True)  # keeps each line's own ending, so offsets stay exact
    want = [l.rstrip() for l in search.replace(CRLF, LF).split(LF)]
    hits = [k for k in range(len(raw) - len(want) + 1) if [l.rstrip() for l in raw[k:k + len(want)]] == want]
    if len(hits) != 1:
        return None
    start = sum(len(l) for l in raw[:hits[0]])
    last = raw[hits[0] + len(want) - 1]
    end = start + sum(len(l) for l in raw[hits[0]:hits[0] + len(want)]) - (len(last) - len(last.rstrip(CRLF)))
    return start, end


def preview(repo_path: str, edit: dict) -> dict:
    """Check one edit against the working tree and render its diff, without writing."""
    root = Path(repo_path).resolve()
    out = {**edit, "ok": False, "diff": "", "error": None, "new_file": not edit["search"].strip()}
    p = _target(root, edit["path"])
    if p is None:
        out["error"] = "path is outside the repo"
        return out
    if out["new_file"]:
        if p.exists():
            out["error"] = "file already exists (SEARCH was empty)"
            return out
        old, new = "", edit["replace"] + (LF if not edit["replace"].endswith(LF) else "")
    else:
        if not p.is_file():
            out["error"] = "file not found"
            return out
        if p.stat().st_size > MAX_FILE:
            out["error"] = "file too large to edit here"
            return out
        old = _read(p)
        span = _locate(old, edit["search"])
        if not span:
            out["error"] = "the SEARCH text was not found exactly once in the file"
            return out
        rep = edit["replace"].replace(CRLF, LF)
        if CRLF in old:  # match the file's line endings
            rep = rep.replace(LF, CRLF)
        new = old[:span[0]] + rep + old[span[1]:]
    out["ok"] = True
    out["diff"] = "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                               f"a/{edit['path']}", f"b/{edit['path']}", n=2))
    out["_new"] = new
    return out


def apply(repo_path: str, edits: list[dict]) -> dict:
    """Apply edits in order (later edits see earlier ones). Edits that don't match are skipped and reported."""
    root = Path(repo_path).resolve()
    backups: dict[str, str | None] = {}
    results = []
    for e in edits:
        res = preview(repo_path, e)
        if not res["ok"]:
            results.append({k: v for k, v in res.items() if k != "_new"})
            continue
        p = _target(root, e["path"])
        backups.setdefault(e["path"], _read(p) if p.exists() else None)
        p.parent.mkdir(parents=True, exist_ok=True)
        _write(p, res.pop("_new"))
        results.append(res)
    change = {"id": f"c-{uuid.uuid4().hex[:8]}", "backups": backups} if backups else None
    return {"ok": all(r["ok"] for r in results), "results": results, "change": change}


def undo(repo_path: str, change: dict) -> list[str]:
    """Put back the content recorded before an apply (deleting files it created)."""
    root = Path(repo_path).resolve()
    restored = []
    for rel, content in change["backups"].items():
        p = _target(root, rel)
        if p is None:
            continue
        if content is None:
            p.unlink(missing_ok=True)
        else:
            _write(p, content)
        restored.append(rel)
    return restored
