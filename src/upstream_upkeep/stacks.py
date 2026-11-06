"""Demo stacks: run a repo's (or a merge workspace's) Docker Compose stack in isolation.

A project's compose file is written for its real home (fixed container names,
fixed host ports, data under /mnt/user/appdata, secrets in an env file that
is not in git). A demo stack is a copy that can run next to the real one:

  * missing required variables get generated demo values (kept, so they stay
    the same across runs), and env files that do not exist are replaced by a
    generated one,
  * no container names, every published port moves to a random port on
    127.0.0.1, its own network, restarts off,
  * bind mounts outside the project folder go to a throwaway demo data folder
    instead (the project's own files, like config it mounts, are used as is).

Then: start it (in the background: pulls and builds take a while), see what
runs and whether it is healthy, read logs with an error summary, send HTTP
requests to its services, run a command inside one, stop it. Requests only go
to the stack's own published ports.

The repo setting stack.enabled gates all of it; only the user turns it on.
Running a stack builds and runs the project's own code on this machine.
"""
from __future__ import annotations

import json
import re
import secrets
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

import requests
import yaml

from . import config, store

COMPOSE_FILES = ("compose.yaml", "compose.yml", "docker-compose.yaml", "docker-compose.yml")
_MISSING = re.compile(r"required variable (\w+) is missing")
_ERRORS = re.compile(r"\b(error|fatal|panic|exception|traceback|refused|denied|failed|cannot|unable)\b", re.I)
_locks: dict[str, threading.Lock] = {}
MAX_BODY = 20000


class StackError(ValueError):
    pass


def _run(args: list[str], cwd: str | None = None, env: dict | None = None, timeout: int = 120) -> subprocess.CompletedProcess:
    import os
    full_env = {**os.environ, **(env or {})}
    try:
        return subprocess.run(args, cwd=cwd, env=full_env, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, 124, e.stdout or "", (e.stderr or "") + f"\ntimed out after {timeout}s")
    except FileNotFoundError:
        raise StackError("docker is not installed on this machine")


def docker_ok() -> tuple[bool, str]:
    res = _run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=20)
    if res.returncode != 0:
        return False, "Docker isn't running on this machine (start the Docker service)"
    return True, res.stdout.strip()


# ------------------------------------------------------------------ identity
def settings(repo: dict) -> dict:
    s = dict(repo.get("stack") or {})
    s.setdefault("enabled", False)
    s.setdefault("compose_file", "")
    s.setdefault("profiles", [])
    s.setdefault("env", {})
    s.setdefault("services", [])
    s.setdefault("extra_files", [])  # overrides layered on top, e.g. docker/compose.proxy-ca.yml
    return s


def _scope_dir(repo: dict, ws: str | None) -> Path:
    from . import workspaces
    return workspaces.path_of(repo, ws) if ws else Path(repo["path"])


def _name(repo: dict, ws: str | None) -> str:
    raw = f"upkeep-{repo['id']}-{ws or 'main'}".lower()
    return re.sub(r"[^a-z0-9_-]+", "-", raw)[:60].strip("-")


def _home(repo: dict, ws: str | None) -> Path:
    d = config.CONFIG_PATH.parent / "stacks" / repo["id"] / (ws or "main")
    d.mkdir(parents=True, exist_ok=True)
    return d


def compose_file(repo: dict, ws: str | None = None) -> Path | None:
    root = _scope_dir(repo, ws)
    chosen = settings(repo)["compose_file"]
    if chosen:
        p = (root / chosen).resolve()
        if root.resolve() not in p.parents or not p.is_file():
            raise StackError(f"compose file {chosen} not found in the project")
        return p
    return next((root / f for f in COMPOSE_FILES if (root / f).is_file()), None)


# ------------------------------------------------------------------ preparing
def _env_values(repo: dict) -> dict:
    s = settings(repo)
    return {**(repo.get("stack_generated") or {}), **s["env"]}


def _save_generated(repo: dict, gen: dict) -> None:
    with store.lock:
        live = store.get_repo(repo["id"])
        if live is not None:
            live["stack_generated"] = {**(live.get("stack_generated") or {}), **gen}
            store.save_repo(live)
    repo["stack_generated"] = {**(repo.get("stack_generated") or {}), **gen}


def _demo_value(name: str) -> str:
    n = name.upper()
    if n.endswith(("_USER", "_USERNAME", "_LOGIN")):
        return "demo"
    if n.endswith(("_EMAIL",)):
        return "demo@example.test"
    if n.endswith(("_PORT",)):
        return "8080"
    if n.endswith(("_URL", "_HOST")):
        return "http://localhost"
    return "demo-" + secrets.token_hex(12)


def prepare(repo: dict, ws: str | None = None) -> dict:
    """Resolve the compose file into the isolated demo model, written to the stack's folder."""
    cf = compose_file(repo, ws)
    if not cf:
        raise StackError("no compose file in this project (compose.yaml or docker-compose.yml)")
    root = _scope_dir(repo, ws).resolve()
    home = _home(repo, ws)
    demo_env = home / "demo.env"
    files = [cf]
    for extra in settings(repo)["extra_files"]:
        p = (root / extra).resolve()
        if root not in p.parents or not p.is_file():
            raise StackError(f"extra compose file {extra} not found in the project")
        files.append(p)
    sources = []
    for n, f in enumerate(files):
        source = yaml.safe_load(f.read_text()) or {}
        # env files that are not in the checkout (secrets kept outside git) -> the generated demo env
        for svc in (source.get("services") or {}).values():
            ef = svc.get("env_file") if isinstance(svc, dict) else None
            if ef is None:
                continue
            kept = []
            for it in (ef if isinstance(ef, list) else [ef]):
                path = it.get("path") if isinstance(it, dict) else it
                if path and (f.parent / path).is_file():
                    kept.append(str((f.parent / path).resolve()))
            svc["env_file"] = kept + [str(demo_env)]
        src = home / f"source-{n}.yml"
        src.write_text(yaml.safe_dump(source, sort_keys=False))
        sources += ["-f", str(src)]
    profiles = sum((["--profile", p] for p in settings(repo)["profiles"]), [])
    base = ["docker", "compose", "--project-directory", str(root), *sources, *profiles]
    generated = {}
    for _ in range(30):
        env = _env_values(repo) | generated
        demo_env.write_text("".join(f"{k}={v}\n" for k, v in sorted(env.items())))
        res = _run([*base, "config", "--format", "json"], cwd=str(root), env=env, timeout=60)
        missing = _MISSING.findall(res.stderr)
        if res.returncode == 0:
            break
        if not missing:
            raise StackError((res.stderr or res.stdout).strip()[-800:] or "docker compose config failed")
        for name in missing:
            generated[name] = _demo_value(name)
    else:
        raise StackError("could not fill in the compose file's required variables")
    if generated:
        _save_generated(repo, generated)
    model = json.loads(res.stdout)
    wanted = set(settings(repo)["services"])
    services = model.get("services") or {}
    if wanted:
        keep = set()
        stack = list(wanted & set(services))
        while stack:  # keep what the chosen services depend on
            n = stack.pop()
            if n not in keep:
                keep.add(n)
                stack += [d for d in (services[n].get("depends_on") or {}) if d in services]
        model["services"] = services = {k: v for k, v in services.items() if k in keep}
    data = home / "data"
    moved = []
    for name, svc in services.items():
        svc.pop("container_name", None)
        if svc.get("build"):
            # Built images get the demo's own name: a demo (or workspace) build must never replace
            # an image the real install runs, like allglance/gateway:latest.
            svc["image"] = f"{_name(repo, ws)}-{name}:demo"
        svc["restart"] = "no"
        svc.pop("env_file", None)  # already inlined into environment by config
        ports = []
        for p in svc.get("ports") or []:
            if isinstance(p, dict):
                ports.append({"target": p["target"], "protocol": p.get("protocol", "tcp"), "host_ip": "127.0.0.1"})  # no published port: docker picks a free one
        if ports:
            svc["ports"] = ports
        for v in svc.get("volumes") or []:
            if v.get("type") != "bind":
                continue
            srcp = Path(v["source"])
            if srcp == root or root in srcp.parents:
                continue  # the project's own files
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", str(srcp).strip("/"))
            dest = data / safe
            looks_file = bool(srcp.suffix) and not srcp.is_dir()
            if looks_file:
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.touch(exist_ok=True)
            else:
                dest.mkdir(parents=True, exist_ok=True)
            dest.chmod(0o777)  # demo data: services often run as another uid
            v["source"] = str(dest)
            v.setdefault("bind", {})["create_host_path"] = True
            moved.append({"service": name, "from": str(srcp), "to": str(dest)})
    for net in (model.get("networks") or {}).values():
        if isinstance(net, dict) and not net.get("external"):
            net.pop("name", None)
    # Secret and config files outside the project (a proxy CA for builds, say): Docker's builder
    # refuses to read those unless allowed. They are listed so the Stack tab can show them.
    outside = sorted({str(Path(x["file"])) for kind in ("secrets", "configs") for x in (model.get(kind) or {}).values()
                      if isinstance(x, dict) and x.get("file") and root not in Path(x["file"]).resolve().parents})
    model["name"] = _name(repo, ws)
    out = home / "compose.json"
    out.write_text(json.dumps(model, indent=1))
    meta = {"compose_file": str(cf.relative_to(root)), "extra_files": [str(f.relative_to(root)) for f in files[1:]],
            "project": _name(repo, ws), "root": str(root),
            "services": sorted(services), "generated_env": sorted(generated) or sorted(repo.get("stack_generated") or {}),
            "moved_mounts": moved, "outside_files": outside}
    (home / "meta.json").write_text(json.dumps(meta, indent=1))
    return meta


# ------------------------------------------------------------------ running
def _compose(repo: dict, ws: str | None, *args: str, timeout: int = 120) -> subprocess.CompletedProcess:
    home = _home(repo, ws)
    if not (home / "compose.json").is_file():
        raise StackError("the stack has not been prepared yet: start it first")
    meta = json.loads((home / "meta.json").read_text())
    root = meta["root"]
    # Builds may read the listed outside files (meta["outside_files"]); nothing else changes.
    env = {"BUILDX_BAKE_ENTITLEMENTS_FS": "0"} if meta.get("outside_files") else None
    return _run(["docker", "compose", "-p", _name(repo, ws), "--project-directory", root, "-f", str(home / "compose.json"), *args],
                cwd=root, env=env, timeout=timeout)


def _check_enabled(repo: dict) -> None:
    if not settings(repo)["enabled"]:
        raise StackError("demo stacks are off for this repo: turn them on in its Stack tab")


def _lock(key: str) -> threading.Lock:
    return _locks.setdefault(key, threading.Lock())


def up(repo: dict, ws: str | None = None, build: bool = False) -> dict:
    """Prepare and start the stack in the background. Progress goes to the stack's op log.

    Images that are missing are built either way; build=True rebuilds them (after changing code)."""
    _check_enabled(repo)
    ok, msg = docker_ok()
    if not ok:
        raise StackError(msg)
    meta = prepare(repo, ws)
    home = _home(repo, ws)
    lock = _lock(_name(repo, ws))
    if not lock.acquire(blocking=False):
        raise StackError("this stack is already starting or stopping")
    log = home / "op.log"
    with log.open("a") as f:  # keep earlier runs: the reason a previous start failed matters
        f.write(f"\n{config.now_iso()} starting {meta['project']} ({', '.join(meta['services'])})\n")

    def run():
        try:
            for attempt in range(3):
                res = _compose(repo, ws, "up", "-d", *(["--build"] if build else []), "--wait", "--wait-timeout", "300",
                               timeout=1500)
                out = (res.stdout or "") + (res.stderr or "")
                with log.open("a") as f:
                    f.write(out)
                    f.write(f"\n{config.now_iso()} {'started' if res.returncode == 0 else 'FAILED (exit %d)' % res.returncode}\n")
                # A registry saying "too many requests" is temporary (Docker Hub limits anonymous pulls).
                if res.returncode == 0 or "429 Too Many Requests" not in out or attempt == 2:
                    break
                with log.open("a") as f:
                    f.write(f"{config.now_iso()} the registry is rate limiting; trying again in {30 * (attempt + 1)}s\n")
                time.sleep(30 * (attempt + 1))
        finally:
            lock.release()
    threading.Thread(target=run, daemon=True).start()
    return {**meta, "starting": True}


def down(repo: dict, ws: str | None = None, reset_data: bool = False) -> dict:
    home = _home(repo, ws)
    if not (home / "compose.json").is_file():
        return {"ok": True, "detail": "not running"}
    res = _compose(repo, ws, "down", "--remove-orphans", *(["-v"] if reset_data else []), timeout=300)
    if reset_data:
        shutil.rmtree(home / "data", ignore_errors=True)
        with store.lock:
            live = store.get_repo(repo["id"])
            if live is not None:
                live.pop("stack_generated", None)
                store.save_repo(live)
    with (home / "op.log").open("a") as f:
        f.write(f"{config.now_iso()} stopped{' and reset its data' if reset_data else ''}\n")
    return {"ok": res.returncode == 0, "detail": (res.stderr or res.stdout).strip()[-600:]}


def status(repo: dict, ws: str | None = None) -> dict:
    home = _home(repo, ws)
    meta = json.loads((home / "meta.json").read_text()) if (home / "meta.json").is_file() else None
    op = (home / "op.log").read_text().splitlines()[-25:] if (home / "op.log").is_file() else []
    busy = _lock(_name(repo, ws)).locked()
    out = {"enabled": settings(repo)["enabled"], "project": _name(repo, ws), "prepared": meta is not None,
           "busy": busy, "op_log": op, "services": [], "meta": meta}
    try:
        cf = compose_file(repo, ws)
        out["compose_file"] = str(cf.relative_to(_scope_dir(repo, ws))) if cf else None
    except StackError as e:
        out["compose_file"], out["error"] = None, str(e)
    if not meta:
        return out
    res = _compose(repo, ws, "ps", "-a", "--format", "json", timeout=30)
    rows = []
    for line in res.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        rows += parsed if isinstance(parsed, list) else [parsed]
    for r in rows:
        ports = [{"target": p.get("TargetPort"), "published": p.get("PublishedPort"),
                  "url": f"http://127.0.0.1:{p['PublishedPort']}" if p.get("PublishedPort") else None}
                 for p in r.get("Publishers") or [] if p.get("PublishedPort")]
        out["services"].append({"service": r.get("Service"), "state": r.get("State"), "health": r.get("Health") or None,
                                "status": r.get("Status"), "exit_code": r.get("ExitCode"), "ports": ports,
                                "image": r.get("Image")})
    running = [s for s in out["services"] if s["state"] == "running"]
    unhealthy = [s["service"] for s in out["services"] if s["health"] == "unhealthy" or s["state"] in ("exited", "dead")]
    out["summary"] = ("not started" if not out["services"] else
                      f"{len(running)}/{len(meta['services'])} running" + (f"; problems: {', '.join(unhealthy)}" if unhealthy else ""))
    return out


def logs(repo: dict, ws: str | None = None, service: str | None = None, tail: int = 200) -> dict:
    args = ["logs", "--no-color", "--timestamps", "--tail", str(max(1, min(tail, 2000)))]
    if service:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", service):
            raise StackError("bad service name")
        args.append(service)
    res = _compose(repo, ws, *args, timeout=60)
    text = res.stdout + res.stderr
    errs: dict[str, list[str]] = {}
    for line in text.splitlines():
        if _ERRORS.search(line):
            svc = line.split("|", 1)[0].strip() if "|" in line else (service or "?")
            errs.setdefault(svc, []).append(line.strip()[-300:])
    return {"text": text[-60000:], "errors": {k: {"count": len(v), "last": v[-5:]} for k, v in errs.items()}}


def _port_for(repo: dict, ws: str | None, service: str, port: int | None) -> int:
    st = status(repo, ws)
    svc = next((s for s in st["services"] if s["service"] == service), None)
    if not svc:
        raise StackError(f"no service {service} in this stack")
    if svc["state"] != "running":
        raise StackError(f"{service} is {svc['state']}")
    pubs = svc["ports"]
    if not pubs:
        raise StackError(f"{service} publishes no port (use exec to reach it from inside)")
    hit = next((p for p in pubs if port in (None, p["target"])), None)
    if not hit:
        raise StackError(f"{service} has no published port {port}; it has {[p['target'] for p in pubs]}")
    return int(hit["published"])


def request(repo: dict, ws: str | None, service: str, path: str = "/", method: str = "GET", port: int | None = None,
            headers: dict | None = None, body=None) -> dict:
    """Send one HTTP request to a service of the stack (its published port on 127.0.0.1)."""
    method = (method or "GET").upper()
    if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"):
        raise StackError("bad method")
    if not path.startswith("/"):
        path = "/" + path
    pub = _port_for(repo, ws, service, port)
    url = f"http://127.0.0.1:{pub}{path}"
    kw = {"headers": {str(k): str(v) for k, v in (headers or {}).items()}, "timeout": 30, "allow_redirects": False}
    if isinstance(body, (dict, list)):
        kw["json"] = body
    elif body is not None:
        kw["data"] = str(body).encode()
    t0 = time.time()
    try:
        r = requests.request(method, url, **kw)
    except requests.RequestException as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "url": url}
    text = r.text
    out = {"ok": True, "url": url, "status": r.status_code, "ms": int((time.time() - t0) * 1000),
           "headers": {k: v for k, v in r.headers.items() if k.lower() in ("content-type", "location", "content-length", "server", "www-authenticate")},
           "body": text[:MAX_BODY], "truncated": len(text) > MAX_BODY}
    try:
        out["json"] = r.json() if "json" in r.headers.get("content-type", "") and len(text) <= MAX_BODY else None
    except ValueError:
        out["json"] = None
    return out


def exec_(repo: dict, ws: str | None, service: str, command: str | list[str], timeout: int = 120) -> dict:
    """Run a command inside a running service (no shell unless the command asks for one)."""
    if not re.fullmatch(r"[A-Za-z0-9._-]+", service or ""):
        raise StackError("bad service name")
    argv = command if isinstance(command, list) else shlex.split(command or "")
    if not argv:
        raise StackError("empty command")
    res = _compose(repo, ws, "exec", "-T", service, *map(str, argv), timeout=max(5, min(timeout, 600)))
    return {"exit_code": res.returncode, "stdout": res.stdout[-20000:], "stderr": res.stderr[-8000:]}
