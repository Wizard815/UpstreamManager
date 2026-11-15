"""Edge cases from a review: odd folders, damaged state, symlinks, unknown chats, wrong input types."""
import json
import os
import shutil
from pathlib import Path

import pytest

from upstream_upkeep import components, config, registry, store, workspaces

from conftest import commit_file, git


def test_a_folder_inside_a_repo_is_not_a_repo(client, repos):
    up, mine = repos
    (mine / "docs").mkdir()
    r = client.post("/api/add", json={"path": str(mine / "docs")})
    assert r.status_code == 400 and "not a git repo" in r.get_json()["error"]


def test_a_repo_whose_git_dir_vanished_is_never_acted_on(client, repos, tmp_path):
    up, mine = repos
    inner = mine / "inner"
    inner.mkdir()
    git(inner, "init", "-q", "-b", "main")
    commit_file(inner, "x.txt", "x\n", "inner")
    registry.add_repo(str(inner), None, "inner")
    shutil.rmtree(inner / ".git")  # now a plain folder inside 'mine'
    before = git(mine, "rev-parse", "HEAD")
    commit_file(up, "b.txt", "b\n", "upstream moves")
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert res[0]["status"] == "error" and "no longer a git repository" in res[0]["error"]
    assert git(mine, "rev-parse", "HEAD") == before


def test_a_moved_repo_folder_does_not_break_the_dashboard(client, repos, tmp_path):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    mine.rename(tmp_path / "moved")
    r = client.get("/api/repos")
    assert r.status_code == 200 and "missing or no longer a git repository" in r.get_json()[0]["error"]
    assert client.get("/api/repo/mine/commits").status_code == 200
    assert client.get("/api/repo/mine/changes").status_code == 200


def test_a_damaged_state_file_is_kept_not_wiped(tmp_path, monkeypatch):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"repos": [{"id": "a"}], "settings": {"x": 1}})[:-3])
    monkeypatch.setattr(config, "CONFIG_PATH", f)
    with pytest.raises(RuntimeError, match="could not be read"):
        config.load()
    kept = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(kept) == 1 and kept[0].read_text().startswith('{"repos": [{"id": "a"}]')


def test_same_folder_name_gets_its_own_id(tmp_path, repos):
    _, mine = repos
    other = tmp_path / "x" / "mine"
    other.parent.mkdir()
    git(tmp_path / "x", "init", "-q", "-b", "main", "mine")
    commit_file(other, "o.txt", "o\n", "other")
    a = registry.add_repo(str(mine), None, None)
    b = registry.add_repo(str(other), None, None)
    assert (a["id"], b["id"]) == ("mine", "mine-2")
    assert registry.add_repo(str(mine), None, None)["id"] == "mine"  # re-adding keeps its id


def test_detached_head_falls_back_to_a_real_branch(repos):
    _, mine = repos
    git(mine, "checkout", "-q", "--detach")
    assert registry.add_repo(str(mine), None, "mine")["focus_branch"] == "main"


def test_a_failed_encode_never_empties_the_file(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/file", data='{"path": "a.txt", "content": "one\\ud800two"}', content_type="application/json")
    assert r.status_code == 400 and Path(mine, "a.txt").read_text() == "one\ntwo\nthree\n"


def test_symlinks_are_renamed_and_deleted_as_links(client, repos, tmp_path):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    os.symlink("a.txt", mine / "link")
    assert client.post("/api/repo/mine/files/rename", json={"path": "link", "to": "link2"}).get_json()["ok"]
    assert (mine / "link2").is_symlink() and (mine / "a.txt").is_file()
    assert client.post("/api/repo/mine/files/delete", json={"path": "link2"}).get_json()["ok"]
    assert not (mine / "link2").is_symlink() and (mine / "a.txt").read_text() == "one\ntwo\nthree\n"
    outside = tmp_path / "secret.txt"
    outside.write_text("s\n")
    os.symlink(outside, mine / "out")
    for call in (("file", {"path": "out", "content": "x"}), ("files/delete", {"path": "out"}),
                 ("files/rename", {"path": "out", "to": "o2"})):
        assert client.post(f"/api/repo/mine/{call[0]}", json=call[1]).status_code == 400
    assert outside.read_text() == "s\n"


def test_writing_under_a_file_is_a_clear_400(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/file", json={"path": "a.txt/b", "content": "x"})
    assert r.status_code == 400 and "is a file" in r.get_json()["error"]


def test_unknown_chats_are_404_not_silently_new(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    sid = client.post("/api/repo/mine/sessions", json={}).get_json()["session"]
    store.add_chat_message("mine", sid, "user", "keep me active")
    client.delete("/api/repo/mine/sessions/s-gone", json={})
    assert client.get("/api/chat/mine?session=s-gone").status_code == 404
    assert client.post("/api/repo/mine/sessions/s-gone/agent-note", json={"text": "hi"}).status_code == 404
    assert client.post("/api/repo/mine/sessions/s-gone/tags", json={"tags": []}).status_code == 404
    assert registry.get_repo("mine")["active_session"] == sid


def test_tagged_chats_survive_starting_a_new_one(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    sid = client.post("/api/repo/mine/sessions", json={}).get_json()["session"]
    client.post(f"/api/repo/mine/sessions/{sid}/tags", json={"tags": [{"type": "pr", "id": "1", "label": "#1"}]})
    client.post("/api/repo/mine/sessions", json={})
    assert sid in [s["id"] for s in client.get("/api/repo/mine/sessions").get_json()]


@pytest.mark.parametrize("message", ["", "   \n ", 5, None])
def test_blank_or_odd_messages_are_400(client, repos, message):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    assert client.post("/api/repo/mine/chat", json={"message": message}).status_code == 400


def test_a_changed_submodule_url_is_followed(tmp_path):
    def lib(name, extra=False):
        r = tmp_path / name
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        commit_file(r, "l.txt", "1\n", "base")
        return r
    a, b = lib("libA"), lib("libB")
    hub = tmp_path / "hub"
    hub.mkdir()
    git(hub, "init", "-q", "-b", "main")
    commit_file(hub, "h.txt", "h\n", "hub")
    git(hub, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(a), "lib")
    git(hub, "commit", "-qm", "add lib")
    git(b, "fetch", "-q", str(a), "main")
    git(b, "reset", "-q", "--hard", "FETCH_HEAD")
    commit_file(b, "new.txt", "n\n", "only in B")
    git(hub, "config", "-f", ".gitmodules", "submodule.lib.url", str(b))
    git(hub, "commit", "-qam", "point lib at B")
    repo = registry.add_repo(str(hub), None, "hub")
    comp = repo["components"][0]
    os.environ["GIT_ALLOW_PROTOCOL"] = "file"
    try:
        st = components.compare(str(hub), comp, do_fetch=True)
    finally:
        os.environ.pop("GIT_ALLOW_PROTOCOL", None)
    assert st["url"] == str(b) and st["behind"] == 1


def test_change_workspaces_in_other_scripts_stay_apart(client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    one = client.post("/api/repo/mine/workspaces", json={"change": "修复登录"}).get_json()["workspace"]
    two = client.post("/api/repo/mine/workspaces", json={"change": "测试缓存"}).get_json()["workspace"]
    assert one["name"] != two["name"] and two["change"] == "测试缓存"


@pytest.mark.parametrize("method,path,body", [
    ("post", "/api/repo/mine/file", [1, 2]),
    ("post", "/api/repo/mine/file", {"path": 5, "content": "x"}),
    ("post", "/api/repo/mine/files/new", {"path": "a\u0000b"}),
    ("post", "/api/repo/mine/files/delete", "a.txt"),
    ("post", "/api/add", {"path": 5}),
    ("post", "/api/repo/mine/focus", {"branch": 5}),
    ("get", "/api/repo/mine/commits?skip=abc", None),
    ("post", "/api/repo/mine/memory/index-commits", {"count": "x"}),
    ("post", "/api/settings", [1]),
])
def test_wrong_shapes_are_400_not_500(client, repos, method, path, body):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = getattr(client, method)(path, json=body) if body is not None else getattr(client, method)(path)
    assert 400 <= r.status_code < 500, (r.status_code, r.get_data(as_text=True)[:200])
