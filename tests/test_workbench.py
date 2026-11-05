"""General chats, chat tags, the file editor's endpoints and uncommitted-change marks."""
from pathlib import Path

from upstream_upkeep import llm, registry, store

from conftest import commit_file, git


def test_general_chat_sessions(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    sid = client.post("/api/repo/_general/sessions", json={}).get_json()["session"]
    store.add_chat_message("_general", sid, "user", "Which repo is behind?")
    hist = client.get(f"/api/chat/_general?session={sid}").get_json()
    assert hist["repo_id"] == "_general" and hist["messages"][0]["content"] == "Which repo is behind?"
    msgs = llm.build_messages(store.chat_owner("_general"), store.get_session("_general", sid), "which?")
    assert "Watched repositories" in msgs[0]["content"] and "mine (id mine" in msgs[0]["content"]
    chats = client.get("/api/chats").get_json()
    assert [(c["repo_id"], c["title"]) for c in chats] == [("_general", "Which repo is behind?")]
    assert "_general" not in [r["id"] for r in client.get("/api/repos").get_json()]


def test_chat_tags_reach_the_model(client, repos):
    up, mine = repos
    commit_file(mine, "x.py", "print(1)\n", "add x")
    registry.add_repo(str(mine), None, "mine")
    sha = git(mine, "rev-parse", "HEAD")
    commit_file(up, "b.txt", "b\n", "up")
    commit_file(mine, "m.txt", "m\n", "mine 2")
    client.post("/api/run-daily", json={})
    sid = client.post("/api/repo/mine/sessions", json={}).get_json()["session"]
    r = client.post(f"/api/repo/mine/sessions/{sid}/tags", json={"tags": [
        {"type": "commit", "id": sha, "label": "add x"},
        {"type": "request", "id": "mine:main", "label": "merge request"},
        {"type": "pr", "id": "12", "label": "#12 Fix login", "url": "https://github.com/o/r/pull/12"},
        {"type": "bogus", "id": "x"}]}).get_json()
    assert [t["type"] for t in r["tags"]] == ["commit", "request", "pr"]
    system = llm.build_messages(registry.get_repo("mine"), store.get_session("mine", sid), "what is this?")[0]["content"]
    assert "This chat is about" in system and "print(1)" in system
    assert "Request mine:main: merge, pending" in system and "pull/12" in system
    listed = next(c for c in client.get("/api/chats").get_json() if c["id"] == sid)
    assert len(listed["tags"]) == 3


def test_general_chat_can_tag_another_repos_commit(client, repos):
    _, mine = repos
    commit_file(mine, "y.py", "secret_value = 2\n", "add y")
    registry.add_repo(str(mine), None, "mine")
    sid = client.post("/api/repo/_general/sessions", json={}).get_json()["session"]
    client.post(f"/api/repo/_general/sessions/{sid}/tags",
                json={"tags": [{"type": "commit", "id": git(mine, "rev-parse", "HEAD"), "repo": "mine"}]})
    system = llm.build_messages(store.chat_owner("_general"), store.get_session("_general", sid), "?")[0]["content"]
    assert "secret_value = 2" in system


def test_editor_save_detects_changes_on_disk(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    f = client.get("/api/repo/mine/file?path=a.txt").get_json()
    ok = client.post("/api/repo/mine/file", json={"path": "a.txt", "content": "new\n", "mtime": f["mtime"]}).get_json()
    assert ok["ok"] and Path(mine, "a.txt").read_text() == "new\n"
    stale = client.post("/api/repo/mine/file", json={"path": "a.txt", "content": "older\n", "mtime": f["mtime"]})
    assert stale.status_code == 409 and stale.get_json()["changed"]
    forced = client.post("/api/repo/mine/file", json={"path": "a.txt", "content": "older\n", "mtime": f["mtime"], "force": True})
    assert forced.get_json()["ok"] and Path(mine, "a.txt").read_text() == "older\n"


def test_file_manager_new_rename_delete(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    assert client.post("/api/repo/mine/files/new", json={"path": "src/lib", "dir": True}).get_json()["ok"]
    assert client.post("/api/repo/mine/files/new", json={"path": "src/lib/x.py"}).get_json()["ok"]
    assert client.post("/api/repo/mine/files/new", json={"path": "src/lib/x.py"}).status_code == 409
    r = client.post("/api/repo/mine/files/rename", json={"path": "src/lib/x.py", "to": "src/y.py"}).get_json()
    assert r["ok"] and Path(mine, "src/y.py").exists() and not Path(mine, "src/lib/x.py").exists()
    assert client.post("/api/repo/mine/files/rename", json={"path": "src", "to": "src/inner"}).status_code == 400
    assert client.post("/api/repo/mine/files/delete", json={"path": "src"}).get_json()["ok"]
    assert not Path(mine, "src").exists()


def test_file_manager_refuses_git_dir_root_and_escapes(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    for path in (".git/config", ".GIT/hooks/x", "", "../outside.txt", "/etc/passwd"):
        assert client.post("/api/repo/mine/files/new", json={"path": path}).status_code == 400, path
        assert client.post("/api/repo/mine/files/delete", json={"path": path}).status_code == 400, path
        assert client.post("/api/repo/mine/file", json={"path": path, "content": "x"}).status_code == 400, path
    assert client.post("/api/repo/mine/files/rename", json={"path": "a.txt", "to": ".git/a.txt"}).status_code == 400
    assert Path(mine, ".git", "config").exists() and Path(mine, "a.txt").exists()


def test_changes_marks_uncommitted_files(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    Path(mine, "a.txt").write_text("changed\n")
    Path(mine, "new dir").mkdir()
    Path(mine, "new dir", "n.txt").write_text("n\n")
    git(mine, "mv", "a.txt", "b.txt")
    items = {i["path"]: i["status"] for i in client.get("/api/repo/mine/changes").get_json()["items"]}
    assert items == {"b.txt": "R", "new dir/n.txt": "?"}
