"""References library; herdr and Hermes Studio as destinations; a task's branch: changes, review, commit."""
import io
import json
import os
import socket
import tempfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from upstream_upkeep import credentials, llm, references, registry, store, tasks

from conftest import commit_file, git


@pytest.fixture
def lib(tmp_path):
    store.save_settings({"references": {"root": str(tmp_path / "refs")}})
    return tmp_path / "refs"


def _pdf_bytes(text: str) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(300, 300)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): w._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
    page[NameObject("/Contents")] = w._add_object(stream)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _upload(client, folder, name, data):
    return client.post("/api/refs/upload", data={"folder": folder, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", headers={"X-Upstream-Upload": "1"})


def test_folders_uploads_and_reading_for_models(client, lib):
    assert client.post("/api/refs/folder", json={"parent": "", "name": "GPU"}).get_json()["ok"]
    assert client.post("/api/refs/folder", json={"parent": "GPU", "name": "papers"}).get_json()["ok"]
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("docs/notes.txt", "kernel tuning notes")
    png = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000")
    for name, data in [("tuning.pdf", _pdf_bytes("Wave64 occupancy matters")), ("bundle.zip", zbuf.getvalue()),
                       ("board.png", png), ("readme.md", b"# Notes\nsee the pdf")]:
        r = _upload(client, "GPU/papers", name, data).get_json()
        assert r["ok"], r
    # a second file of the same name is kept beside it
    assert _upload(client, "GPU/papers", "readme.md", b"again").get_json()["saved"][0]["path"] == "GPU/papers/readme (2).md"
    # uploads need the custom header (a form post from another site can't add it)
    bad = client.post("/api/refs/upload", data={"folder": "", "file": (io.BytesIO(b"x"), "x.txt")}, content_type="multipart/form-data")
    assert bad.status_code == 415
    items = {i["name"]: i for i in client.get("/api/refs?path=GPU/papers").get_json()["items"]}
    assert items["tuning.pdf"]["kind"] == "pdf" and items["bundle.zip"]["kind"] == "zip" and items["board.png"]["kind"] == "image"
    assert "Wave64 occupancy" in client.get("/api/refs/read?path=GPU/papers/tuning.pdf").get_json()["text"]
    assert "docs/notes.txt" in client.get("/api/refs/read?path=GPU/papers/bundle.zip").get_json()["text"]
    assert client.get("/api/refs/read?path=GPU/papers/bundle.zip!docs/notes.txt").get_json()["text"] == "kernel tuning notes"
    over = client.get("/api/refs/read?path=GPU").get_json()["text"]
    assert "papers/tuning.pdf" in over and "# Notes" in over
    # models get the text; images go as pictures only to a model that can see
    text, imgs = references.for_model("GPU/papers/board.png")
    assert imgs and imgs[0]["url"].startswith("data:image/png;base64,")
    repo = {"id": store.GENERAL}
    store.save_settings({"llm": {"vision": True}})
    msgs = llm.build_messages(repo, {"messages": []}, "what's in these?", [{"type": "ref", "id": "GPU/papers/tuning.pdf"},
                                                                          {"type": "ref", "id": "GPU/papers/board.png"}])
    content = msgs[-1]["content"]
    assert isinstance(content, list) and "Wave64" in content[0]["text"] and content[1]["type"] == "image_url"
    store.save_settings({"llm": {"vision": False}})
    assert isinstance(llm.build_messages(repo, {"messages": []}, "x", [{"type": "ref", "id": "GPU/papers/board.png"}])[-1]["content"], str)


def test_moving_sorting_and_safety(client, lib):
    client.post("/api/refs/folder", json={"parent": "", "name": "a"})
    client.post("/api/refs/folder", json={"parent": "", "name": "b"})
    _upload(client, "a", "x.txt", b"hello")
    assert client.post("/api/refs/move", json={"path": "a/x.txt", "to": "b"}).get_json()["path"] == "b/x.txt"
    assert client.post("/api/refs/move", json={"path": "a", "to": "a"}).status_code == 400
    for bad in ["../etc", "/etc/passwd", "a/../../x", ".refs.json", "b/.git/config"]:
        assert client.get(f"/api/refs/read?path={bad}").status_code in (400, 404), bad
    # a symlink out of the library is refused
    os.symlink("/etc", lib / "out")
    assert client.get("/api/refs/read?path=out/hostname").status_code == 400
    assert client.post("/api/refs/delete", json={"path": "b"}, headers={"X-Upstream-Client": "agent"}).status_code == 403
    assert client.post("/api/refs/delete", json={"path": "b"}).get_json()["ok"] and not (lib / "b").exists()
    assert client.get("/api/refs/search?q=hel").get_json()["ok"]


def test_clone_a_reference_repo_branch_and_switch(client, lib, repos):
    up, mine = repos
    git(up, "branch", "feature")
    r = client.post("/api/refs/clone", json={"url": f"file://{up}", "folder": "", "name": "upstream-ref"}).get_json()
    assert r["ok"] and r["state"] == "cloning"
    for _ in range(400):
        item = next((i for i in client.get("/api/refs").get_json()["items"] if i["name"] == "upstream-ref"), {})
        if item.get("kind") == "repo" and "job" not in item:
            break
        time.sleep(0.05)
    assert item["branch"] == "main" and item["source"] == f"file://{up}"
    assert "feature" in client.get("/api/refs/branches?path=upstream-ref").get_json()["items"]
    assert client.post("/api/refs/switch", json={"path": "upstream-ref", "branch": "feature"}).get_json()["branch"] == "feature"
    assert client.post("/api/refs/branch", json={"path": "upstream-ref", "name": "my-notes"}).get_json()["branch"] == "my-notes"
    assert client.post("/api/refs/branch", json={"path": "upstream-ref", "name": "--bad"}).status_code == 400
    # your own files can't be moved into a cloned repo
    _upload(client, "", "n.txt", b"x")
    assert client.post("/api/refs/move", json={"path": "n.txt", "to": "upstream-ref"}).status_code == 400
    # a failed clone says why and can be dismissed
    r = client.post("/api/refs/clone", json={"url": "file:///no/such/repo", "name": "broken"}).get_json()
    for _ in range(400):
        item = next((i for i in client.get("/api/refs").get_json()["items"] if i["name"] == "broken"), {})
        if item.get("job", {}).get("state") == "failed":
            break
        time.sleep(0.05)
    assert item["job"]["error"]
    client.post("/api/refs/dismiss", json={"path": "broken"})
    assert not any(i["name"] == "broken" for i in client.get("/api/refs").get_json()["items"])


# ---------------------------------------------------------------- herdr
@pytest.fixture
def fake_herdr():
    d = tempfile.mkdtemp(prefix="hd")
    path = os.path.join(d, "h.sock")
    calls = []
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(path)
    srv.listen(8)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                req = json.loads(c.makefile().readline())
                calls.append(req)
                m, res = req["method"], {}
                if m == "ping":
                    res = {"type": "pong", "version": "0.9", "protocol": 1}
                elif m == "workspace.create":
                    res = {"type": "workspace_created", "workspace": {"workspace_id": "w7"}, "tab": {}, "root_pane": {"pane_id": "w7:p1"}}
                elif m == "agent.start":
                    res = {"type": "agent_started", "agent": {"name": req["params"]["name"]}, "argv": []}
                elif m == "agent.get":
                    res = {"type": "agent_info", "agent": {"agent_status": "working", "cwd": "/x"}}
                elif m == "agent.read":
                    res = {"type": "pane_read", "read": {"text": "Editing parse.py…"}}
                c.sendall((json.dumps({"id": req["id"], "result": res}) + "\n").encode())

    threading.Thread(target=serve, daemon=True).start()
    yield path, calls
    srv.close()


def test_hand_a_task_to_a_herdr_agent(client, repos, fake_herdr):
    path, calls = fake_herdr
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": path, "kind": "codex"}})
    assert client.get("/api/integrations").get_json()["herdr"]["ok"]
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Fix parse", "kind": "bug"}).get_json()["task"]
    assert client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr"}, headers={"X-Upstream-Client": "agent"}).status_code == 400
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr"}).get_json()
    assert r["ok"], r
    ws_call = next(c for c in calls if c["method"] == "workspace.create")["params"]
    assert ws_call["cwd"] == r["where"]["workdir"] != str(mine) and ws_call["focus"] is False
    start = next(c for c in calls if c["method"] == "agent.start")["params"]
    assert start["kind"] == "codex" and start["pane_id"] == "w7:p1"
    prompt = next(c for c in calls if c["method"] == "agent.prompt")["params"]["text"]
    assert "Do not commit" in prompt and tasks.COMMIT_FILE in prompt and "Fix parse" in prompt
    a = client.get(f"/api/tasks/{t['id']}/agent").get_json()
    assert a["status"] == "working" and "parse.py" in a["output"] and a["run"]["running"]
    # a follow-up goes to the same agent, no new workspace
    client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr", "message": "add a test too"})
    assert [c["method"] for c in calls].count("workspace.create") == 1
    assert calls[-1]["method"] == "agent.prompt" and calls[-1]["params"]["text"] == "add a test too"


def test_herdr_not_running_is_explained(client, repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    client.post("/api/integrations/settings", json={"herdr": {"enabled": True, "socket": "/nonexistent/h.sock"}})
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "x"}).get_json()["task"]
    r = client.post(f"/api/tasks/{t['id']}/send", json={"to": "herdr"}).get_json()
    assert not r["ok"] and "herdr isn't running" in r["error"]


# ---------------------------------------------------------------- Studio
class _Studio(BaseHTTPRequestHandler):
    calls: list = []
    msgs: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        type(self).calls.append(("GET", self.path, None, self.headers.get("Authorization")))
        if self.headers.get("Authorization") != "Bearer jwt-1":
            return self._send(401, {"error": "auth"})
        if self.path.startswith("/api/studio/sessions/conversations/"):
            return self._send(200, {"messages": type(self).msgs.get(self.path.split("/")[5], [])})
        return self._send(200, {"sessions": []})

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        type(self).calls.append(("POST", self.path, body, self.headers.get("Authorization")))
        if self.path == "/api/auth/login":
            return self._send(200, {"token": "jwt-1"} if body.get("password") == "pw" else {})
        if self.path == "/api/studio/chat-run/runs":
            type(self).msgs[body["session_id"]] = [{"role": "user", "content": body["input"]},
                                                   {"role": "assistant", "content": "Done; message in .upkeep-commit-msg"}]
            return self._send(200, {"ok": True, "status": "completed", "session_id": body["session_id"]})
        self._send(404, {})


def test_hand_a_task_to_hermes_studio(client, repos):
    _Studio.calls, _Studio.msgs = [], {}
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Studio)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    try:
        r = client.post("/api/integrations/settings", json={"studio": {"enabled": True, "url": url, "agent": "claude-code"},
                                                            "studio_login": {"username": "me", "password": "nope"}})
        assert r.status_code == 400
        client.post("/api/integrations/settings", json={"studio": {"enabled": True, "url": url, "agent": "claude-code"},
                                                        "studio_login": {"username": "me", "password": "pw"}})
        assert client.get("/api/integrations").get_json()["studio"]["ok"]
        t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Port the fetcher", "kind": "feature", "to": "studio"}).get_json()
        assert t["ok"], t
        sid = t["where"]["studio"]
        assert t["where"]["url"].endswith(f"/#/hermes/session/{sid}")
        for _ in range(400):
            a = client.get(f"/api/tasks/{t['task']['id']}/agent").get_json()
            if not a["run"]["running"] and len(a["messages"]) == 2:
                break
            time.sleep(0.05)
        run = next(c for c in _Studio.calls if c[1] == "/api/studio/chat-run/runs")
        assert run[2]["workspace"] == t["where"]["workdir"] and run[2]["coding_agent_id"] == "claude-code"
        assert "Do not commit" in run[2]["instructions"] and run[3] == "Bearer jwt-1"
    finally:
        srv.shutdown()
        credentials.delete("studio_token")


# ---------------------------------------------------------------- suggestions and the task's branch
def test_suggestions_follow_the_kind_and_size_of_the_work():
    av = {"herdr": True, "wiki": True, "orb": True}
    t = {"repo_id": "r", "kind": "idea", "body": "", "about": []}
    assert tasks.suggest(t, av)["to"] == "wiki"
    t = {"repo_id": "r", "kind": "fix", "body": "", "about": [{"type": "commit"}]}
    assert tasks.suggest(t, av)["to"] == "here"
    t = {"repo_id": "r", "kind": "merge", "body": "Conflicts:\n" + "\n".join(f"- f{i}.go" for i in range(17)), "about": []}
    s = tasks.suggest(t, av)
    assert s["to"] == "herdr" and "17 files" in s["why"]
    assert tasks.suggest(t, {})["to"] == "here"
    assert tasks.suggest({"repo_id": store.GENERAL, "kind": "fix", "body": "", "about": []}, av)["to"] == "orb"


def test_review_and_commit_on_the_task_branch(client, repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    git(mine, "branch", "release")
    assert client.post("/api/tasks", json={"repo": repo["id"], "title": "x", "onto": "nope"}).status_code == 400
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Tidy a.txt", "kind": "fix", "onto": "release", "to": "here"}).get_json()
    assert t["ok"], t
    ch = client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()
    assert ch["onto"] == "release" and ch["files"] == [] and ch["commits"] == []
    wpath = Path(ch["path"])
    (wpath / "a.txt").write_text("one\nTWO\nthree\n")
    (wpath / "new.txt").write_text("hi\n")
    (wpath / tasks.COMMIT_FILE).write_text("Tidy a.txt\n\nUppercase the second line.")
    ch = client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()
    assert {f["path"] for f in ch["files"]} == {"a.txt", "new.txt"} and ch["proposed_message"].startswith("Tidy a.txt")
    d = client.get(f"/api/tasks/{t['task']['id']}/diff?path=a.txt").get_json()["diff"]
    assert "+TWO" in d and "-two" in d
    assert "+hi" in client.get(f"/api/tasks/{t['task']['id']}/diff?path=new.txt").get_json()["diff"]
    assert client.post(f"/api/tasks/{t['task']['id']}/commit", json={"message": " "}).status_code == 400
    assert client.post(f"/api/tasks/{t['task']['id']}/commit", json={"message": "x"}, headers={"X-Upstream-Client": "agent"}).status_code == 403
    r = client.post(f"/api/tasks/{t['task']['id']}/commit", json={"message": "Tidy a.txt (edited by me)\n\nWhy: consistency."}).get_json()
    assert r["ok"], r
    assert git(wpath, "log", "-1", "--format=%B").startswith("Tidy a.txt (edited by me)")
    assert not (wpath / tasks.COMMIT_FILE).exists() and git(wpath, "status", "--porcelain") == ""
    ch = client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()
    assert len(ch["commits"]) == 1 and {f["path"] for f in ch["files"]} == {"a.txt", "new.txt"} and ch["ready"]
    # the user's own checkout never changed
    assert (mine / "a.txt").read_text() == "one\ntwo\nthree\n"
    s = client.post(f"/api/tasks/{t['task']['id']}/submit", json={}).get_json()
    assert s["ok"] and s["request"]["focus_branch"] == "release"


def test_a_task_chat_edits_the_task_branch(client, repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Edit", "kind": "fix", "to": "here"}).get_json()
    sid = t["where"]["session"]
    reply = "EDIT a.txt\n<<<<<<< SEARCH\ntwo\n=======\n2\n>>>>>>> REPLACE"
    store.add_chat_message(repo["id"], sid, "assistant", reply, extra={"edits": [{"path": "a.txt", "ok": True}]})
    r = client.post(f"/api/repo/{repo['id']}/edits/apply", json={"session": sid, "index": 0}).get_json()
    assert r["ok"], r
    ws = Path(client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()["path"])
    assert (ws / "a.txt").read_text() == "one\n2\nthree\n" and (mine / "a.txt").read_text() == "one\ntwo\nthree\n"


def test_a_half_resolved_merge_is_never_committed(client, repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    commit_file(up, "a.txt", "one\nUP\nthree\n", "upstream edit")
    commit_file(mine, "a.txt", "one\nMINE\nthree\n", "my edit")
    git(mine, "fetch", "-q", "origin")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Merge origin", "kind": "merge", "upstream": "origin", "to": "here"}).get_json()
    assert t["ok"], t
    ws = Path(client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()["path"])
    assert "<<<<<<<" in (ws / "a.txt").read_text()
    r = client.post(f"/api/tasks/{t['task']['id']}/commit", json={"message": "merge"})
    assert r.status_code == 409 and "a.txt" in r.get_json()["error"]
    (ws / "a.txt").write_text("one\nMINE and UP\nthree\n")
    r = client.post(f"/api/tasks/{t['task']['id']}/commit", json={"message": "Merge origin, keeping both edits"}).get_json()
    assert r["ok"], r
    assert len(git(ws, "log", "-1", "--format=%P").split()) == 2  # a real merge commit
