"""Smoke test for chat + memory against a running Upstream Management app.

Start the app with an isolated state file, then point this at it:

    UPSTREAM_STATE_FILE=data/localtest/state.json PORT=5055 uv run python -m upstream_upkeep.run
    uv run python scripts/local_smoke.py --repo /path/to/a/git/repo --llm http://127.0.0.1:8081/v1

It configures the app's own model connection (never hermes config), then checks:
model listing, repo add, commit indexing, a grounded chat reply, preference
capture and recall across a new session, search, notes, and persistence of
sessions. Needs a real model on --llm; takes a few minutes on a small one.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import urllib.error
import urllib.request

results: list[tuple[str, bool, str]] = []


def call(base: str, method: str, path: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(
        base + path, method=method, headers={"Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.urlopen(req, timeout=900) as f:
            return json.loads(f.read())
    except urllib.error.HTTPError as e:
        return {"http_error": e.code}


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}  {detail}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", default="http://127.0.0.1:5055")
    ap.add_argument("--repo", required=True, help="path to a local git repo with some history")
    ap.add_argument("--llm", required=True, help="OpenAI-compatible base URL, e.g. http://127.0.0.1:8081/v1")
    ap.add_argument("--api-key", default="")
    ap.add_argument("--model", default="", help="model id; default is the first the server lists")
    a = ap.parse_args()
    app, rid = a.app, "smoke-repo"
    R = f"/api/repo/{rid}"

    models = call(app, "GET", f"/api/models?base_url={a.llm}&api_key={a.api_key}")
    ids = [m["id"] for m in models.get("data", [])]
    check("model list from server", bool(ids), models.get("error") or f"{len(ids)} models")
    if not ids:
        return 1
    model = a.model or ids[0]
    call(app, "POST", "/api/settings", {"llm": {"base_url": a.llm, "api_key": a.api_key, "model": model}})
    saved = call(app, "GET", "/api/settings").get("llm", {})
    check("connection saved in app settings", saved.get("model") == model and saved.get("base_url") == a.llm)

    st = call(app, "GET", "/api/memory/status")
    check("memory running", st.get("ok") and not st.get("error"), str(st.get("error") or st.get("embed_provider")))

    add = call(app, "POST", "/api/add", {"path": a.repo.replace("\\", "/"), "name": rid})
    check("repo added", add.get("ok") or "already" in json.dumps(add).lower(), json.dumps(add)[:120])

    idx = call(app, "POST", f"{R}/memory/index-commits", {})
    check("index commits", idx.get("ok") and idx.get("total", 0) > 0, f"added {idx.get('added')}, total {idx.get('total')}")

    call(app, "POST", f"{R}/sessions", {})
    t = time.time()
    r1 = call(app, "POST", f"{R}/chat", {"message": "What changed recently in this repo? Name specific commits."})
    first = r1.get("session")
    first_session = first
    check("chat reply not empty", r1.get("ok") and len(r1.get("reply", "")) > 40, f"{time.time() - t:.0f}s")
    print("      reply:", (r1.get("reply") or r1.get("error", ""))[:300].replace("\n", " "))
    shas = {c["memory"].split()[1] for c in call(app, "GET", f"{R}/memory").get("items", []) if c["kind"] == "commit"}
    check("reply cites a real commit", any(s in r1.get("reply", "") for s in shas))

    call(app, "POST", f"{R}/chat", {"message": "Remember this: I prefer small PRs that touch only one module."})
    t = time.time()
    found = False
    while time.time() - t < 180 and not found:
        time.sleep(10)
        found = any("small" in i["memory"].lower() and i["kind"] == "chat"
                    for i in call(app, "GET", f"{R}/memory").get("items", []))
    check("preference extracted to memory", found, f"{time.time() - t:.0f}s")
    call(app, "POST", f"{R}/sessions", {})
    check("new chat starts empty", not call(app, "GET", f"/api/chat/{rid}").get("messages"))
    r2 = call(app, "POST", f"{R}/chat", {"message": "How should I split my next PR?"})
    reply = r2.get("reply", "").lower()
    check("new chat recalls preference", r2.get("ok") and ("one module" in reply or "single module" in reply or "small" in reply))
    print("      reply:", reply[:300].replace("\n", " "))

    call(app, "POST", f"{R}/sessions", {"session": first})
    check("old session restored", len(call(app, "GET", f"/api/chat/{rid}?session={first}").get("messages", [])) >= 2)

    own = call(app, "GET", f"{R}/memory?q=recent%20changes")
    allr = call(app, "GET", f"{R}/memory?q=recent%20changes&scope=all")
    check("memory search (repo / all)", own.get("ok") and allr.get("ok") and own.get("items"))

    note = call(app, "POST", f"{R}/memory", {"text": "Never force-push to main"})
    nid = (note.get("items") or [{}])[0].get("id")
    check("note added", bool(nid))
    if nid:
        call(app, "DELETE", f"/api/memory/{nid}")
        check("note removed", not any(i["id"] == nid for i in call(app, "GET", f"{R}/memory").get("items", [])))

    time.sleep(30)  # let background extraction of the later turns finish
    chat_mems = [i["memory"] for i in call(app, "GET", f"{R}/memory").get("items", []) if i["kind"] == "chat"]
    check("extracted memories are about the user, not restated commits",
          not any(re.search(r"\b[0-9a-f]{8}\b", m) for m in chat_mems), "; ".join(chat_mems)[:200])
    n_commits = sum(1 for i in call(app, "GET", f"{R}/memory").get("items", []) if i["kind"] == "commit")
    re_add = call(app, "POST", "/api/add", {"path": a.repo.replace("\\", "/"), "name": rid})
    again = call(app, "POST", f"{R}/memory/index-commits", {})
    check("re-adding a repo does not re-index commits", re_add.get("ok") and again.get("added") == 0, f"added {again.get('added')}, commits {n_commits}")

    cmp_ = call(app, "GET", f"{R}/compare")
    check("compare returns upstream counts", cmp_.get("ok") and cmp_["plan"]["upstreams"] and "behind" in cmp_["plan"]["upstreams"][0])
    cm = call(app, "GET", f"{R}/commits?view=all&limit=5")
    check("commit list", cm.get("ok") and len(cm.get("items", [])) == 5)
    d = call(app, "GET", f"{R}/commit/{cm['items'][0]['full']}") if cm.get("items") else {}
    check("commit detail", d.get("ok") and d.get("patch") is not None)
    tr = call(app, "GET", f"{R}/tree?path=")
    check("file tree", tr.get("ok") and tr.get("entries"))
    first = next((e for e in tr.get("entries", []) if e["type"] == "file"), None)
    check("file viewer", bool(first) and call(app, "GET", f"{R}/file?path={first['path']}").get("ok"))
    check("path traversal blocked", call(app, "GET", f"{R}/file?path=../../etc/passwd").get("http_error") == 404)
    cl = call(app, "POST", f"{R}/memory/cleanup", {})
    check("memory cleanup", cl.get("ok"), json.dumps(cl.get("reasons")))
    sid = call(app, "POST", f"{R}/sessions", {}).get("session")
    check("delete chat", call(app, "DELETE", f"{R}/sessions/{first_session}").get("ok"))

    bad = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed")
    print("Now restart the app and confirm sessions and memories survive (manual).")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
