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
import os
import uuid
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
LIST_LIMIT = 2000
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


def set_root(path: str) -> str:
    """Move the library to another folder (an absolute path; created if missing; not inside a tracked repo)."""
    p = Path(str(path or "").strip()).expanduser()
    if not p.is_absolute():
        raise RefError("give the library an absolute folder, like /data/references")
    for r in store.all_repos():
        rp = Path(r["path"]).resolve()
        if p.resolve() == rp or rp in p.resolve().parents:
            raise RefError(f"that folder is inside the repo {r.get('name') or r['id']}: pick one outside your repos")
    try:
        p.mkdir(parents=True, exist_ok=True)
        probe = p / ".write-test.tmp"
        probe.write_text("x")
        probe.unlink()
    except OSError as e:
        raise RefError(f"can't use {p}: {e}")
    store.save_settings({"references": {**(store.settings().get("references") or {}), "root": str(p)}})
    return str(p)


def root() -> Path:
    r = (store.settings().get("references") or {}).get("root") or str(config.CONFIG_PATH.parent / "references")
    p = Path(r).expanduser()
    p.mkdir(parents=True, exist_ok=True)
    return p


def norm(rel: str | None) -> str:
    """The one spelling of a library path ("./a//b/" is "a/b"; "." and "" are the library itself)."""
    if rel is None:
        return ""
    if not isinstance(rel, str) or "\0" in rel or rel.startswith(("/", "\\")):
        raise RefError("bad path")
    return "/".join(x for x in Path(rel).parts if x not in ("", "."))


def _p(rel: str | None, must_exist: bool = True) -> Path:
    """A path inside the library; never outside it, never inside a clone's .git."""
    rel = norm(rel)
    parts = [x for x in Path(rel).parts if x]
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
    if gitops._run(["git", "rev-parse", "--verify", "-q", "HEAD"], cwd=str(p)).returncode != 0:
        return {"incomplete": True, "branch": None, "branches": [], "url": gitops._run(["git", "remote", "get-url", "origin"], cwd=str(p)).stdout.strip(),
                "error": "this clone has no commits checked out (it may have been cut off): delete it and clone again"}
    out = {"branch": gitops._run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=str(p)).stdout.strip(),
           "url": gitops._run(["git", "remote", "get-url", "origin"], cwd=str(p)).stdout.strip()}
    out["branches"] = [b for b in gitops._run(["git", "for-each-ref", "--format=%(refname:short)", "refs/heads"], cwd=str(p)).stdout.split() if b][:50]
    return out


def tree(rel: str = "") -> dict:
    rel = norm(rel)
    d = _p(rel)
    if not d.is_dir() or kind_of(d) == "repo" and rel and False:
        raise RefError("not a folder")
    meta, items = _meta(), []
    with _LOCK:
        jobs = dict(_JOBS)
    children = sorted((c for c in d.iterdir() if c.name not in (META, ".git") and not c.name.endswith((".part", ".tmp"))),
                      key=lambda x: (not x.is_dir(), x.name.lower()))
    more = max(0, len(children) - LIST_LIMIT)
    for c in children[:LIST_LIMIT]:
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
    return {"path": rel, "root": str(root()), "items": items, "in_repo": _inside_repo(d), "more": more}


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
    if not _NAME.fullmatch(name) or name in (".", "..") or name.casefold() == ".git" or name == META \
            or name.endswith((".part", ".tmp")):
        raise RefError("names can't be empty, longer than 120 characters, contain / or \\, or be .git")
    return name


def _upload_name(filename: str) -> str:
    """A browser's file name made safe to keep: no folders, no control characters, at most 120 characters."""
    name = re.sub(r"[\\/\x00-\x1f:]+", "_", (filename or "").strip()).strip(" .") or "upload"
    if name.casefold() == ".git" or name == META:
        name = "_" + name
    if len(name) > 120:
        stem, dot, ext = name.rpartition(".")
        ext = ext if dot and len(ext) <= 10 else ""
        name = (stem if dot else name)[: 119 - len(ext)] + ("." + ext if ext else "")
    return name.removesuffix(".part").removesuffix(".tmp") or "upload"


def mkdir(parent: str, name: str) -> dict:
    parent = norm(parent)
    if _inside_repo(_p(parent)):
        raise RefError("keep your own folders out of cloned repos")
    d = _p(parent) / _clean_name(name)
    if d.exists():
        raise RefError(f"{name} already exists")
    d.mkdir()
    _note(_rel(d), added=config.now_iso())
    return {"path": _rel(d)}


def clone(url: str, folder: str = "", name: str | None = None, branch: str | None = None) -> dict:
    """Clone a repo into a folder of the library, in the background (history only; files on demand)."""
    url = registry.parse_git_url(url)
    folder = norm(folder)
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
                _JOBS[rel] = {**_JOBS[rel], "state": "failed", "error": _explain((r.stderr or r.stdout).strip(), branch)}
        if r.returncode == 0:
            _note(rel, source=url, added=config.now_iso())
        else:
            shutil.rmtree(dest, ignore_errors=True)

    threading.Thread(target=run, daemon=True, name=f"ref-clone-{name}").start()
    return {"path": rel, "state": "cloning"}


def _explain(err: str, branch: str | None = None) -> str:
    """git's clone/fetch errors in plain words."""
    if "could not read Username" in err or "Authentication failed" in err or "Repository not found" in err:
        return "the repo wasn't found, or it's private (the host asked for a sign-in): check the address"
    if "Remote branch" in err and "not found" in err or "couldn't find remote ref" in err:
        return f"the repo has no branch {branch}" if branch else "that branch doesn't exist on the repo"
    if "Could not resolve host" in err or "unable to access" in err:
        return "can't reach the host: check the address and your network"
    if "timed out" in err:
        return "the clone took too long and was stopped"
    return err[-500:] or "the clone failed"


def dismiss_job(rel: str) -> None:
    with _LOCK:
        _JOBS.pop(rel, None)


def _repo(rel: str) -> Path:
    p = _p(norm(rel))
    if kind_of(p) != "repo":
        raise RefError(f"{rel} is not a cloned repo")
    return p


def branch(rel: str, name: str, start: str | None = None) -> dict:
    """Make your own branch of a reference repo (and switch to it), e.g. to try changes or keep notes."""
    p = _repo(rel)
    if not re.fullmatch(r"[A-Za-z0-9._/-]{1,100}", name or "") or name.startswith(("-", "/")) or ".." in name:
        raise RefError("branch names may use letters, digits and . _ / -")
    if start and (start.startswith("-") or not re.fullmatch(r"[A-Za-z0-9._/@{}^~-]{1,200}", start)):
        raise RefError("start the branch from a branch name or commit")
    r = gitops._run(["git", "switch", "-c", name] + (["--end-of-options", start] if start else []), cwd=str(p))
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
        raise RefError(_explain((r.stderr or r.stdout).strip(), name) if "remote ref" in (r.stderr or "") else
                       (r.stderr or r.stdout).strip() or f"no branch {name}")
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
    d = _p(norm(folder))
    if not d.is_dir():
        raise RefError("upload into a folder")
    if _inside_repo(d):
        raise RefError("keep your own files out of cloned repos: upload into a folder")
    name = _upload_name(filename)
    for old in d.glob("*.part"):  # uploads cut off long ago (a closed tab, a crash)
        try:
            if time.time() - old.stat().st_mtime > 3600:
                old.unlink()
        except OSError:
            pass
    tmp = d / f".{uuid.uuid4().hex}.part"
    written = 0
    with open(tmp, "wb") as out:
        while chunk := stream.read(1024 * 1024):
            written += len(chunk)
            if written > MAX_UPLOAD:
                out.close()
                tmp.unlink(missing_ok=True)
                raise RefError(f"{name} is larger than {MAX_UPLOAD // 2**20} MB")
            out.write(chunk)
    dest = d / name
    stem, suf, n = dest.stem, dest.suffix, 2
    while True:  # claim the name atomically: two uploads of the same name keep both
        try:
            os.link(tmp, dest)
            break
        except FileExistsError:
            dest = d / f"{stem} ({n}){suf}"
            n += 1
    tmp.unlink()
    rel = _rel(dest)
    _note(rel, added=config.now_iso(), uploaded=True)
    return {"path": rel, "size": written, "kind": kind_of(dest)}


def move(rel: str, to_folder: str, new_name: str | None = None) -> dict:
    rel, to_folder = norm(rel), norm(to_folder)
    if not rel:
        raise RefError("can't move the library itself")
    src = _p(rel)
    with _LOCK:
        if any(j == rel or j.startswith(rel + "/") for j, v in _JOBS.items() if v["state"] == "cloning"):
            raise RefError("a clone is still running in there: wait for it to finish")
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
    rel = norm(rel)
    if not rel:
        raise RefError("can't delete the library itself")
    p = _p(rel)
    with _LOCK:
        if any(j == rel or j.startswith(rel + "/") for j, v in _JOBS.items() if v["state"] == "cloning"):
            raise RefError("a clone is still running in there: wait for it to finish")
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    else:
        p.unlink()
    _move_meta(rel, None)


def set_note(rel: str, note: str) -> None:
    rel = norm(rel)
    if not rel:
        raise RefError("pick an item")
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
    rel = norm(rel)
    p = _p(rel)
    k = kind_of(p)
    out = {"path": rel, "kind": k, "size": p.stat().st_size if p.is_file() else None, **_meta().get(rel, {})}
    try:
        if inner is not None:
            return {**out, "path": f"{rel}!{inner}", "kind": "text", "text": _zip_member(p, inner, limit)}
        if k in ("folder", "repo"):
            return {**out, "text": folder_overview(rel, limit)}
        if k == "pdf":
            return {**out, "text": _pdf_text(p, limit)}
        if k == "zip":
            return {**out, "text": _zip_listing(p)}
    except KeyError:
        raise RefError(f"there is no {inner} in {rel}")
    except (zipfile.BadZipFile, zipfile.LargeZipFile) as e:
        raise RefError(f"{rel} isn't a readable zip: {e}")
    except RefError:
        raise
    except Exception as e:  # pypdf and friends raise their own kinds for damaged files
        raise RefError(f"{rel} can't be read: {type(e).__name__}: {str(e)[:200]}")
    if k in ("image", "video"):
        return {**out, "text": None, "mime": mimetypes.guess_type(p.name)[0]}
    with open(p, "rb") as fh:
        data = fh.read(limit * 4)
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
            if f.lower().startswith("readme") and len(depth) <= 1 and len(readmes) < 6 and not (Path(dirpath) / f).is_symlink():
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
    base = rel.split("!", 1)[0] if "!" in rel and rel.split("!", 1)[0].lower().endswith(".zip") else rel
    rel = norm(base) + rel[len(base):]
    p = _p(norm(base))
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
    """Find files by name, and text in cloned repos (git grep). Skips .git and dependency folders."""
    q = (query or "").strip()
    if len(q) < 2:
        raise RefError("search for at least 2 characters")
    base, out, repos, seen = root(), [], [], 0
    for dirpath, dirnames, filenames in os.walk(base):
        if ".git" in dirnames:
            repos.append(Path(dirpath))
        dirnames[:] = [x for x in dirnames if x not in (".git", "node_modules", ".venv", "__pycache__")]
        for name in dirnames + filenames:
            seen += 1
            if q.lower() in name.lower() and name != META and not name.endswith(".part"):
                p = Path(dirpath) / name
                out.append({"path": _rel(p), "kind": kind_of(p), "match": "name"})
                if len(out) >= limit:
                    return out
        if seen > 200_000:
            break
    for repo in repos[:50]:
        r = gitops._run(["git", "grep", "-z", "-n", "-I", "--max-count=3", "-F", "-e", q], cwd=str(repo), timeout=60)
        for line in r.stdout.splitlines()[: limit - len(out)]:
            f, n, text = (line.split("\0", 2) + ["", ""])[:3]
            out.append({"path": _rel(repo / f), "kind": "text", "match": f"line {n}: {text.strip()[:160]}"})
        if len(out) >= limit:
            break
    return out
