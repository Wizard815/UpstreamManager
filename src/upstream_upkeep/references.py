"""References: a library of things to look things up in, sorted into folders.

Other GitHub (or any git) repos you want as reference are cloned here (history only, file contents
on demand), and can get a branch of their own. Any file can be uploaded next to them: PDFs, zips,
images, videos, notes. A model or agent can be pointed at a folder or a file: `for_model` turns it
into text it can read (PDF text, a zip's listing, a folder's tree and READMEs), and images go to
models that can see them.

Layout: everything lives under one root folder (Settings, default <data dir>/references), as an
ordinary file tree you can also open yourself. `.refs.json` at the root keeps where each item came
from and when it was added."""
from __future__ import annotations

import base64
import json
import mimetypes
import re
import shutil
import subprocess
import threading
import time
import zipfile
from pathlib import Path

from . import config, gitops, registry, store

META = ".refs.json"
MAX_UPLOAD = 512 * 1024 * 1024
TEXT_LIMIT = 60_000
TREE_LIMIT = 400
IMAGE_LIMIT = 8 * 1024 * 1024
_JOBS: dict[str, dict] = {}       # path -> {"state": cloning|failed, "error", "url", "since"}
_LOCK = threading.Lock()
TEXT_EXT = {".md", ".txt", ".rst", ".py", ".js", ".ts", ".tsx", ".jsx", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg",
            ".c", ".h", ".cc", ".cpp", ".hpp", ".rs", ".go", ".java", ".kt", ".swift", ".rb", ".php", ".sh", ".bash",
            ".zsh", ".ps1", ".sql", ".html", ".css", ".scss", ".vue", ".svelte", ".xml", ".csv", ".tsv", ".log",
            ".cmake", ".mk", ".dockerfile", ".env.example", ".lua", ".zig", ".nix", ".tex", ".org", ".ipynb"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg"}
VIDEO_EXT = {".mp4", ".webm", ".mov", ".mkv", ".avi"}


class RefError(ValueError):
    pass


def root() -> Path:
    r = (store.settings().get("references") or {}).get("root") or str(config.CONFIG_PATH.parent / "references")
    p = Path(r).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _p(rel: str | None, must_exist: bool = True) -> Path:
    """A path inside the library; never outside it, never inside a clone's .git."""
    if rel is None:
        rel = ""
    if not isinstance(rel, str) or "\0" in rel or rel.startswith(("/", "\\")):
        raise RefError("bad path")
    parts = [x for x in Path(rel).parts if x not in ("", ".")]
    if ".." in parts or ".git" in {x.casefold() for x in parts} or (parts and parts[0] == META):
        raise RefError("bad path")
    base = root().resolve()
    p = base.joinpath(*parts)
    real = p.resolve()
    if real != base and base not in real.parents:
        raise RefError("that path leads outside the reference library")
    if must_exist and not p.exists():
        raise FileNotFoundError(rel)
    return p


def _rel(p: Path) -> str:
    return p.relative_to(root().resolve() if p.is_absolute() and str(p).startswith(str(root().resolve())) else root()).as_posix() \
        if p != root() else ""


def _meta() -> dict:
    f = root() / META
    try:
        return json.loads(f.read_text()) if f.is_file() else {}
    except (OSError, ValueError):
        return {}


def _save_meta(m: dict) -> None:
    f = root() / META
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1, sort_keys=True))
    tmp.replace(f)


def _note(rel: str, **info) -> None:
    with _LOCK:
        m = _meta()
        m[rel] = {**m.get(rel, {}), **info}
        _save_meta(m)


def _move_meta(old: str, new: str | None) -> None:
    with _LOCK:
        m = _meta()
        out = {}
        for k, v in m.items():
            if k == old or k.startswith(old + "/"):
                if new is not None:
                    out[new + k[len(old):]] = v
            else:
                out[k] = v
        _save_meta(out)


def kind_of(p: Path) -> str:
    if p.is_dir():
        return "repo" if (p / ".git").exists() else "folder"
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        return "image"
    if ext in VIDEO_EXT:
        return "video"
    if ext == ".pdf":
        return "pdf"
    if ext == ".zip":
        return "zip"
    if ext in TEXT_EXT or p.name.lower() in ("readme", "license", "makefile", "dockerfile"):
        return "text"
    return "file"


def _repo_info(p: Path) -> dict:
    out = {"branch": gitops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(p)).stdout.strip(),
           "url": gitops._run(["git", "remote", "get-url", "origin"], cwd=str(p)).stdout.strip()}
    out["branches"] = [b for b in gitops._run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=str(p)).stdout.split() if b][:50]
    return out


def tree(rel: str = "") -> dict:
    d = _p(rel)
    if not d.is_dir() or kind_of(d) == "repo" and rel and False:
        raise RefError("not a folder")
    meta, items = _meta(), []
    with _LOCK:
        jobs = dict(_JOBS)
    for c in sorted(d.iterdir(), key=lambda x: x.name.lower()):
        if c.name in (META, ".git") or c.name.endswith(".tmp") and c.parent == root():
            continue
        r = (Path(rel) / c.name).as_posix() if rel else c.name
        st = c.lstat()
        e = {"name": c.name, "path": r, "kind": kind_of(c), "size": st.st_size if c.is_file() else None,
             "modified": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)), **meta.get(r, {})}
        if r in jobs:
            e.update(job=jobs[r])
        elif e["kind"] == "repo":
            e.update(_repo_info(c))
        items.append(e)
    for r, j in jobs.items():  # clones that haven't made their folder yet
        if Path(r).parent.as_posix() in (rel or ".",) and not any(i["path"] == r for i in items):
            items.append({"name": Path(r).name, "path": r, "kind": "repo", "job": j})
    return {"path": rel, "root": str(root()), "items": items, "in_repo": _inside_repo(d)}


def _inside_repo(p: Path) -> str | None:
    base = root().resolve()
    for x in [p, *p.parents]:
        if x.resolve() == base or base not in x.resolve().parents and x.resolve() != base:
            break
        if (x / ".git").exists():
            return _rel(x)
    return None


_NAME = re.compile(r"^[^/\\\0]{1,120}$")


def _clean_name(name: str) -> str:
    name = (name or "").strip()
    if not _NAME.fullmatch(name) or name in (".", "..") or name.startswith(".git") or name == META:
        raise RefError("names can't be empty, contain / or \\, or start with .git")
    return name


def mkdir(parent: str, name: str) -> dict:
    d = _p(parent) / _clean_name(name)
    if d.exists():
        raise RefError(f"{name} already exists")
    d.mkdir()
    _note(_rel(d), added=config.now_iso())
    return {"path": _rel(d)}


def clone(url: str, folder: str = "", name: str | None = None, branch: str | None = None) -> dict:
    """Clone a repo into a folder of the library, in the background (history only; files on demand)."""
    url = registry.parse_git_url(url)
    parent = _p(folder)
    name = _clean_name(name or re.sub(r"\.git$", "", url.rstrip("/").split("/")[-1].split(":")[-1]) or "repo")
    dest = parent / name
    rel = _rel(dest)
    if dest.exists():
        raise RefError(f"{rel} already exists")
    with _LOCK:
        if rel in _JOBS and _JOBS[rel]["state"] == "cloning":
            raise RefError(f"{rel} is already being cloned")
        _JOBS[rel] = {"state": "cloning", "url": url, "since": config.now_iso(), "error": None}

    def run():
        cmd = ["git", "clone", "-q", "--filter=blob:none"] + (["--branch", branch] if branch else []) + ["--", url, str(dest)]
        r = gitops._run(cmd, cwd=str(parent), timeout=1800)
        with _LOCK:
            if r.returncode == 0:
                _JOBS.pop(rel, None)
            else:
                _JOBS[rel] = {**_JOBS[rel], "state": "failed", "error": (r.stderr or r.stdout).strip()[-500:]}
        if r.returncode == 0:
            _note(rel, source=url, added=config.now_iso())
        else:
            shutil.rmtree(dest, ignore_errors=True)

    threading.Thread(target=run, daemon=True, name=f"ref-clone-{name}").start()
    return {"path": rel, "state": "cloning"}


def dismiss_job(rel: str) -> None:
    with _LOCK:
        _JOBS.pop(rel, None)


def _repo(rel: str) -> Path:
    p = _p(rel)
    if kind_of(p) != "repo":
        raise RefError(f"{rel} is not a cloned repo")
    return p


def branch(rel: str, name: str, start: str | None = None) -> dict:
    """Make your own branch of a reference repo (and switch to it), e.g. to try changes or keep notes."""
    p = _repo(rel)
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name or "") or name.startswith(("-", "/")) or ".." in name:
        raise RefError("branch names may use letters, digits and . _ / -")
    r = gitops._run(["git", "switch", "-c", name] + ([start] if start else []), cwd=str(p))
    if r.returncode:
        raise RefError((r.stderr or r.stdout).strip())
    return _repo_info(p)


def switch(rel: str, name: str) -> dict:
    """Look at another branch of a reference repo (a remote one is checked out as a local branch)."""
    p = _repo(rel)
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name or "") or name.startswith("-"):
        raise RefError("bad branch name")
    if gitops._run(["git", "status", "--porcelain"], cwd=str(p)).stdout.strip():
        raise RefError("this reference repo has uncommitted changes; commit or discard them first")
    r = gitops._run(["git", "switch", name], cwd=str(p), timeout=900)
    if r.returncode:
        f = gitops._run(["git", "fetch", "origin", f"+refs/heads/{name}:refs/remotes/origin/{name}"], cwd=str(p), timeout=900)
        r = gitops._run(["git", "switch", "--track", f"origin/{name}"], cwd=str(p), timeout=900) if f.returncode == 0 else f
    if r.returncode:
        raise RefError((r.stderr or r.stdout).strip() or f"no branch {name}")
    return _repo_info(p)


def remote_branches(rel: str) -> list[str]:
    p = _repo(rel)
    out = gitops._run(["git", "ls-remote", "--heads", "origin"], cwd=str(p), timeout=60).stdout
    return sorted(line.split("refs/heads/", 1)[1] for line in out.splitlines() if "refs/heads/" in line)[:500]


def update(rel: str) -> dict:
    p = _repo(rel)
    r = gitops._run(["git", "pull", "--ff-only", "-q"], cwd=str(p), timeout=1800)
    if r.returncode:
        raise RefError((r.stderr or r.stdout).strip()[-400:] or "update failed")
    _note(rel, updated=config.now_iso())
    return _repo_info(p)


def save_upload(folder: str, filename: str, stream, size: int | None = None) -> dict:
    d = _p(folder)
    if not d.is_dir():
        raise RefError("upload into a folder")
    name = _clean_name(Path(filename or "upload").name)
    dest = d / name
    stem, suf, n = dest.stem, dest.suffix, 2
    while dest.exists():
        dest = d / f"{stem} ({n}){suf}"
        n += 1
    tmp = dest.with_name(dest.name + ".part")
    written = 0
    with open(tmp, "wb") as out:
        while chunk := stream.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD:
                out.close()
                tmp.unlink(missing_ok=True)
                raise RefError(f"{name} is larger than {MAX_UPLOAD // 2**20} MB")
            out.write(chunk)
    tmp.replace(dest)
    rel = _rel(dest)
    _note(rel, added=config.now_iso(), uploaded=True)
    return {"path": rel, "size": written, "kind": kind_of(dest)}


def move(rel: str, to_folder: str, new_name: str | None = None) -> dict:
    src = _p(rel)
    if not rel:
        raise RefError("can't move the library itself")
    dst_dir = _p(to_folder)
    if not dst_dir.is_dir():
        raise RefError("move into a folder")
    dst = dst_dir / _clean_name(new_name or src.name)
    if dst.exists():
        raise RefError(f"{_rel(dst)} already exists")
    if src.is_dir() and (dst.resolve() == src.resolve() or src.resolve() in dst.resolve().parents):
        raise RefError("can't move a folder into itself")
    if _inside_repo(dst_dir) and _inside_repo(dst_dir) != rel:
        raise RefError("keep your own files out of cloned repos: move it into a folder")
    shutil.move(str(src), str(dst))
    _move_meta(rel, _rel(dst))
    return {"path": _rel(dst)}


def delete(rel: str) -> None:
    p = _p(rel)
    if not rel:
        raise RefError("can't delete the library itself")
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()
    _move_meta(rel, None)


def set_note(rel: str, note: str) -> None:
    _p(rel)
    _note(rel, note=(note or "").strip()[:2000])


# ------------------------------------------------------------------ reading, for people and models
def _pdf_text(p: Path, limit: int) -> str:
    from pypdf import PdfReader
    reader = PdfReader(str(p))
    out, total = [], 0
    for i, page in enumerate(reader.pages):
        t = (page.extract_text() or "").strip()
        if t:
            out.append(f"--- page {i + 1} ---\n{t}")
            total += len(t)
        if total > limit:
            out.append(f"...(stopped at page {i + 1} of {len(reader.pages)})")
            break
    return "\n".join(out) or "(no text in this PDF: it may be scanned images)"


def _zip_listing(p: Path) -> str:
    with zipfile.ZipFile(p) as z:
        infos = z.infolist()
        lines = [f"{i.filename}  ({i.file_size} bytes)" for i in infos[:TREE_LIMIT]]
        if len(infos) > TREE_LIMIT:
            lines.append(f"...and {len(infos) - TREE_LIMIT} more")
        return "\n".join(lines)


def _zip_member(p: Path, member: str, limit: int) -> str:
    with zipfile.ZipFile(p) as z:
        info = z.getinfo(member)
        if info.file_size > 20 * 1024 * 1024:
            raise RefError("that file in the zip is too big to read")
        return z.read(info).decode("utf-8", errors="replace")[:limit]


def read(rel: str, limit: int = TEXT_LIMIT) -> dict:
    """What's in an item, as text where possible. "file.zip!inner/path" reads a file inside a zip."""
    inner = None
    if "!" in rel and rel.split("!", 1)[0].lower().endswith(".zip"):
        rel, inner = rel.split("!", 1)
    p = _p(rel)
    k = kind_of(p)
    out = {"path": rel, "kind": k, "size": p.stat().st_size if p.is_file() else None, **_meta().get(rel, {})}
    if inner is not None:
        return {**out, "path": f"{rel}!{inner}", "kind": "text", "text": _zip_member(p, inner, limit)}
    if k in ("folder", "repo"):
        return {**out, "text": folder_overview(rel, limit)}
    if k == "pdf":
        return {**out, "text": _pdf_text(p, limit)}
    if k == "zip":
        return {**out, "text": _zip_listing(p)}
    if k in ("image", "video"):
        return {**out, "text": None, "mime": mimetypes.guess_type(p.name)[0]}
    data = p.read_bytes()[: limit * 4]
    if b"\0" in data[:4096]:
        return {**out, "text": None, "binary": True}
    text = data.decode("utf-8", errors="replace")
    return {**out, "text": text[:limit] + ("\n...(truncated)" if len(text) > limit else "")}


def folder_overview(rel: str, limit: int = TEXT_LIMIT) -> str:
    base = _p(rel)
    lines, readmes, count = [], [], 0
    for dirpath, dirnames, filenames in __import__("os").walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in (".git", "node_modules", "__pycache__", ".venv"))
        depth = Path(dirpath).relative_to(base).parts
        if len(depth) >= 4:
            dirnames[:] = []
        for f in sorted(filenames):
            if f == META:
                continue
            rp = (Path(*depth) / f).as_posix() if depth else f
            if count < TREE_LIMIT:
                lines.append(rp)
            count += 1
            if f.lower().startswith("readme") and len(depth) <= 1 and len(readmes) < 6:
                readmes.append(Path(dirpath) / f)
    head = f"[Reference folder {rel or '(library root)'}: {count} files]\n" + "\n".join(lines)
    if count > TREE_LIMIT:
        head += f"\n...and {count - TREE_LIMIT} more files"
    for r in readmes:
        try:
            head += f"\n\n[{_rel(r)}]\n" + r.read_text(encoding="utf-8", errors="replace")[:6000]
        except OSError:
            pass
    return head[:limit]


def for_model(rel: str, limit: int = 20_000) -> tuple[str, list[dict]]:
    """An item as (text for the prompt, images for a model that can see). Images are data URLs."""
    p = _p(rel.split("!", 1)[0])
    k = kind_of(p)
    if k == "image":
        if p.stat().st_size > IMAGE_LIMIT:
            return f"[Reference image {rel}: too large to send ({p.stat().st_size // 1024} KB)]", []
        mime = mimetypes.guess_type(p.name)[0] or "image/png"
        if mime == "image/svg+xml":
            return f"[Reference image {rel} (SVG)]\n" + p.read_text(errors="replace")[:limit], []
        return f"[Reference image {rel}: attached]", [{"name": rel, "url": f"data:{mime};base64," + base64.b64encode(p.read_bytes()).decode()}]
    if k == "video":
        return f"[Reference video {rel}: {p.stat().st_size // 1024} KB; models here can't watch video, describe what to look for]", []
    r = read(rel, limit)
    if r.get("text") is None:
        return f"[Reference {rel}: a binary file the model can't read]", []
    label = {"pdf": "PDF", "zip": "zip (its file list)", "folder": "folder", "repo": "repo"}.get(r["kind"], "file")
    src = f" from {r['source']}" if r.get("source") else ""
    return f"[Reference {label} {rel}{src}]\n{r['text']}", []


def search(query: str, limit: int = 50) -> list[dict]:
    """Find files by name, and text files/repos by content (git grep in repos, a plain scan elsewhere)."""
    q = (query or "").strip()
    if len(q) < 2:
        raise RefError("search for at least 2 characters")
    base, out = root(), []
    for p in base.rglob("*"):
        if ".git" in p.parts or p.name == META:
            continue
        if q.lower() in p.name.lower():
            out.append({"path": _rel(p), "kind": kind_of(p), "match": "name"})
            if len(out) >= limit:
                return out
    for repo in [d for d in base.rglob(".git")][:50]:
        r = gitops._run(["git", "grep", "-n", "-I", "--max-count=3", "-F", "-e", q], cwd=str(repo.parent), timeout=60)
        for line in r.stdout.splitlines()[: limit - len(out)]:
            f, n, text = (line.split(":", 2) + ["", ""])[:3]
            out.append({"path": _rel(repo.parent / f), "kind": "text", "match": f"line {n}: {text.strip()[:160]}"})
        if len(out) >= limit:
            break
    return out
