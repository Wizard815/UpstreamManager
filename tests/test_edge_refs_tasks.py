"""Edge cases: the reference library's odd files and names, and task branches in odd states."""
import io
import os
import zipfile
from pathlib import Path

import pytest

from upstream_upkeep import references, registry, store, tasks

from conftest import commit_file, git


@pytest.fixture
def lib(tmp_path):
    store.save_settings({"references": {"root": str(tmp_path / "refs")}})
    return tmp_path / "refs"


def up(client, folder, name, data=b"x"):
    return client.post("/api/refs/upload", data={"folder": folder, "file": (io.BytesIO(data), name)},
                       content_type="multipart/form-data", headers={"X-Upstream-Upload": "1"})


# ------------------------------------------------------------ names
@pytest.mark.parametrize("name", [".gitignore", ".github-notes.md", "notes #1 & 2?.md", "ünïcødé 名前.txt", "a" * 119 + ".md"])
def test_ordinary_odd_names_upload_and_read_back(client, lib, name):
    r = up(client, "", name, b"hello").get_json()
    assert r["ok"], r
    path = r["saved"][0]["path"]
    assert client.get("/api/refs/read", query_string={"path": path}).get_json()["text"] == "hello"
    assert client.get("/api/refs/raw", query_string={"path": path}).data == b"hello"


@pytest.mark.parametrize("name", ["..\\..\\evil.txt", "a" * 300 + ".txt", "  spaced  .txt"])
def test_awkward_upload_names_are_made_safe_not_refused(client, lib, name):
    r = up(client, "", name).get_json()
    assert r["ok"], r
    saved = r["saved"][0]["path"]
    assert "/" not in saved and "\\" not in saved and len(saved) <= 120 and (lib / saved).is_file()


@pytest.mark.parametrize("name", [".git", ".GIT", ".refs.json", "", ".", ".."])
def test_reserved_names_are_refused(client, lib, name):
    assert client.post("/api/refs/folder", json={"parent": "", "name": name}).status_code == 400


def test_empty_file_and_part_files(client, lib):
    assert up(client, "", "empty.txt", b"").get_json()["ok"]
    assert client.get("/api/refs/read?path=empty.txt").get_json()["text"] == ""
    (lib / "half.bin.part").write_bytes(b"x")  # an upload cut off by a crash
    assert "half.bin.part" not in [i["name"] for i in client.get("/api/refs").get_json()["items"]]


# ------------------------------------------------------------ broken or hostile files
def test_corrupt_pdf_and_zip_say_so_instead_of_500(client, lib):
    up(client, "", "broken.pdf", b"%PDF-1.4 not really")
    up(client, "", "broken.zip", b"PK\x03\x04 nope")
    zbuf = io.BytesIO()
    with zipfile.ZipFile(zbuf, "w") as z:
        z.writestr("a.txt", "a")
    up(client, "", "ok.zip", zbuf.getvalue())
    for path in ["broken.pdf", "broken.zip", "ok.zip!missing.txt", "ok.zip!../../etc/passwd"]:
        r = client.get("/api/refs/read", query_string={"path": path})
        assert r.status_code in (200, 400, 404), (path, r.status_code)
        assert r.status_code != 200 or r.get_json().get("text") is not None


def test_a_symlinked_readme_in_a_cloned_repo_never_leaks(client, lib, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    repo = lib / "evil"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    os.symlink(secret, repo / "README.md")
    os.symlink(secret, repo / "notes.txt")
    over = client.get("/api/refs/read?path=evil").get_json()["text"]
    assert "TOP-SECRET" not in over
    assert client.get("/api/refs/read?path=evil/notes.txt").status_code == 400
    assert client.get("/api/refs/raw?path=evil/notes.txt").status_code == 404
    text, _ = references.for_model("evil")
    assert "TOP-SECRET" not in text


def test_your_files_stay_out_of_cloned_repos(client, lib):
    repo = lib / "cloned"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    assert up(client, "cloned", "mine.txt").status_code == 400
    assert client.post("/api/refs/folder", json={"parent": "cloned", "name": "mine"}).status_code == 400


def test_a_huge_folder_lists_a_capped_page(client, lib):
    d = lib / "many"
    d.mkdir(parents=True)
    for i in range(2600):
        (d / f"f{i:05}.txt").write_text("x")
    r = client.get("/api/refs?path=many").get_json()
    assert len(r["items"]) <= 2000 and r.get("more") == 600


def test_an_interrupted_clone_is_marked_incomplete(client, lib):
    repo = lib / "half-cloned"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")  # a clone that died before its first checkout: no commits
    item = next(i for i in client.get("/api/refs").get_json()["items"] if i["name"] == "half-cloned")
    assert item.get("incomplete") is True


def test_library_location_can_be_moved_and_is_checked(client, lib, tmp_path, repos):
    upr, mine = repos
    registry.add_repo(str(mine), None, "mine")
    elsewhere = tmp_path / "big-disk" / "library"
    r = client.post("/api/refs/settings", json={"root": str(elsewhere)}).get_json()
    assert r["ok"] and elsewhere.is_dir()
    assert client.get("/api/refs").get_json()["root"] == str(elsewhere)
    assert client.post("/api/refs/settings", json={"root": "relative/path"}).status_code == 400
    assert client.post("/api/refs/settings", json={"root": str(mine / "inside")}).status_code == 400  # inside a tracked repo
    assert client.post("/api/refs/settings", json={"root": str(tmp_path)}, headers={"X-Upstream-Client": "agent"}).status_code == 403


# ------------------------------------------------------------ task branches in odd states
@pytest.fixture
def task_here(client, repos):
    upr, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Edge", "kind": "fix", "to": "here"}).get_json()
    ws = Path(client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()["path"])
    return repo, t["task"], ws, mine


def test_task_diff_never_reads_outside_its_branch(client, task_here, tmp_path):
    repo, t, ws, mine = task_here
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    for p in [str(secret), "../secret.txt", "/etc/passwd"]:
        r = client.get(f"/api/tasks/{t['id']}/diff", query_string={"path": p})
        assert r.status_code == 400 or "TOP-SECRET" not in r.get_data(as_text=True)
        assert "root:" not in r.get_data(as_text=True)


def test_a_symlinked_commit_message_file_is_ignored(client, task_here, tmp_path):
    repo, t, ws, mine = task_here
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET")
    os.symlink(secret, ws / tasks.COMMIT_FILE)
    assert client.get(f"/api/tasks/{t['id']}/changes").get_json()["proposed_message"] == ""


def test_commit_refuses_when_the_agent_switched_branches(client, task_here):
    repo, t, ws, mine = task_here
    git(ws, "switch", "-q", "-c", "agent-went-elsewhere")
    (ws / "a.txt").write_text("changed\n")
    r = client.post(f"/api/tasks/{t['id']}/commit", json={"message": "x"})
    assert r.status_code == 409 and "agent-went-elsewhere" in r.get_json()["error"]


def test_a_deleted_task_branch_folder_is_explained_and_recreated(client, task_here):
    repo, t, ws, mine = task_here
    import shutil
    shutil.rmtree(ws)
    r = client.get(f"/api/tasks/{t['id']}/changes")
    assert r.status_code == 400 and r.get_json()["error"]
    again = client.post(f"/api/tasks/{t['id']}/send", json={"to": "here"}).get_json()
    assert again["ok"], again
    assert Path(client.get(f"/api/tasks/{t['id']}/changes").get_json()["path"]).is_dir()


def test_tasks_of_a_removed_repo_still_load_and_refuse_work_clearly(client, task_here):
    repo, t, ws, mine = task_here
    assert client.delete(f"/api/repo/{repo['id']}", json={}).get_json()["ok"]
    items = client.get("/api/tasks").get_json()["items"]
    assert any(x["id"] == t["id"] for x in items)
    for to in ("here", "herdr", "studio", "hermes"):
        r = client.post(f"/api/tasks/{t['id']}/send", json={"to": to})
        assert r.status_code in (400,) and r.get_json()["error"], (to, r.status_code)
    assert client.get(f"/api/tasks/{t['id']}/changes").status_code in (400, 404)


def test_a_task_on_a_branch_with_slashes_and_unicode(client, repos):
    upr, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    git(mine, "branch", "feature/größe-1")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Ünïcode 修正", "onto": "feature/größe-1", "to": "here"}).get_json()
    assert t["ok"], t
    ch = client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()
    assert ch["onto"] == "feature/größe-1"


def test_branch_quick_look_refuses_odd_names(client, repos):
    upr, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    for b in ["--all", "../x", "HEAD@{1}", "main..origin/main"]:
        r = client.get(f"/api/repo/{repo['id']}/commits", query_string={"view": "branch", "branch": b}).get_json()
        assert r["ok"] is False, b
    assert client.get(f"/api/repo/{repo['id']}/commits", query_string={"view": "branch", "branch": "origin/main"}).get_json()["ok"]


def test_clone_errors_are_in_plain_words():
    assert "private" in references._explain("fatal: could not read Username for 'https://github.com': terminal prompts disabled")
    assert references._explain("warning: Remote branch nope not found in upstream origin", "nope") == "the repo has no branch nope"
    assert "reach the host" in references._explain("fatal: unable to access 'https://x/': Could not resolve host: x")


# ------------------------------------------------------------ from the adversarial review
def test_the_library_itself_can_never_be_deleted_or_moved(client, lib):
    up(client, "", "keep.txt")
    for p in [".", "./", "", "a/..", "./."]:
        r = client.post("/api/refs/delete", json={"path": p})
        assert r.status_code in (400, 404), p
    assert client.post("/api/refs/move", json={"path": ".", "to": ""}).status_code in (400, 404)
    assert (lib / "keep.txt").is_file()


def test_notes_and_sources_follow_odd_spellings(client, lib):
    client.post("/api/refs/folder", json={"parent": "", "name": "keep"})
    client.post("/api/refs/folder", json={"parent": "", "name": "dest"})
    assert client.post("/api/refs/note", json={"path": "./keep/", "note": "why I kept it"}).get_json()["ok"]
    assert client.post("/api/refs/move", json={"path": "keep/", "to": "dest"}).get_json()["path"] == "dest/keep"
    item = client.get("/api/refs?path=dest").get_json()["items"][0]
    assert item["note"] == "why I kept it"


def test_files_with_bangs_in_their_names_can_go_to_a_chat(client, lib):
    up(client, "", "wow!.txt", b"bang")
    text, _ = references.for_model("wow!.txt")
    assert "bang" in text


def test_agents_may_sort_but_not_delete_or_copy_local_repos(client, lib, repos):
    upr, mine = repos
    up(client, "", "x.txt")
    agent = {"X-Upstream-Client": "agent"}
    assert client.post("/api/refs/move", json={"path": "x.txt", "to": "", "name": "y.txt"}, headers=agent).get_json()["ok"]
    assert client.post("/api/refs/delete", json={"path": "y.txt"}, headers=agent).status_code == 403
    for url in [str(mine), f"file://{mine}"]:
        assert client.post("/api/refs/clone", json={"url": url}, headers=agent).status_code == 403
    assert client.post("/api/refs/clone", json={"url": f"file://{mine}", "name": "mine-copy"}).get_json()["ok"]


def test_a_branch_start_cannot_be_an_option(client, lib, repos):
    upr, mine = repos
    repo = lib / "r"
    git(lib.parent, "clone", "-q", str(upr), str(repo)) if lib.exists() else None
    lib.mkdir(exist_ok=True)
    if not repo.exists():
        git(lib, "clone", "-q", str(upr), "r")
    for start in ["--discard-changes", "-f", "--recurse-submodules"]:
        assert client.post("/api/refs/branch", json={"path": "r", "name": "x", "start": start}).status_code == 400
    assert client.post("/api/refs/branch", json={"path": "r", "name": "x", "start": "main"}).get_json()["ok"]


def test_two_uploads_of_the_same_name_keep_both(client, lib):
    a = up(client, "", "same.txt", b"first").get_json()["saved"][0]["path"]
    b = up(client, "", "same.txt", b"second").get_json()["saved"][0]["path"]
    assert a != b and (lib / a).read_bytes() == b"first" and (lib / b).read_bytes() == b"second"
    assert not list(lib.glob("*.part")) and not list(lib.glob(".*.part"))


def test_search_handles_colons_and_accents_in_names(client, lib):
    repo = lib / "r2"
    repo.mkdir(parents=True)
    git(repo, "init", "-q")
    (repo / "a:b ä.txt").write_text("needle here\n")
    git(repo, "add", ".")
    git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "x")
    hits = client.get("/api/refs/search?q=needle").get_json()["items"]
    assert any(h["path"] == "r2/a:b ä.txt" for h in hits)


def test_a_chat_whose_task_branch_is_gone_edits_nothing(client, task_here):
    repo, t, ws, mine = task_here
    sid = t["where"]["session"] if "where" in t and t["where"] else tasks.get(t["id"])["where"]["session"]
    store.add_chat_message(repo["id"], sid, "assistant", "EDIT a.txt\n<<<<<<< SEARCH\ntwo\n=======\n2\n>>>>>>> REPLACE",
                           extra={"edits": [{"path": "a.txt", "ok": True}]})
    name = tasks.get(t["id"])["where"]["workspace"]
    assert client.delete(f"/api/repo/{repo['id']}/workspaces/{name}", json={}).get_json()["ok"]
    r = client.post(f"/api/repo/{repo['id']}/edits/apply", json={"session": sid, "index": 0})
    assert r.status_code == 409
    assert (mine / "a.txt").read_text() == "one\ntwo\nthree\n"
    assert client.get(f"/api/chat/{repo['id']}?session={sid}").status_code == 200  # the chat still opens


def test_conflict_markers_in_oddly_named_files_still_block_the_commit(client, task_here):
    repo, t, ws, mine = task_here
    for name in ["a b.txt", "ä.txt"]:
        (ws / name).write_text("x\n<<<<<<< HEAD\nmine\n=======\ntheirs\n>>>>>>> other\n")
        r = client.post(f"/api/tasks/{t['id']}/commit", json={"message": "x"})
        assert r.status_code == 409 and name in r.get_json()["error"], name
        (ws / name).unlink()
    (ws / "a b.txt").write_text("fine\n")
    files = {f["path"] for f in client.get(f"/api/tasks/{t['id']}/changes").get_json()["files"]}
    assert "a b.txt" in files


def test_merge_tasks_hide_the_agents_message_file(client, repos):
    upr, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    commit_file(upr, "b.txt", "b\n", "upstream moves")
    git(mine, "fetch", "-q", "origin")
    t = client.post("/api/tasks", json={"repo": repo["id"], "title": "Merge", "kind": "merge", "upstream": "origin", "to": "here"}).get_json()
    ws = Path(client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()["path"])
    (ws / tasks.COMMIT_FILE).write_text("Merge origin\n")
    ch = client.get(f"/api/tasks/{t['task']['id']}/changes").get_json()
    assert ch["files"] == [] or all(f["committed"] for f in ch["files"])
    assert ch["proposed_message"] == "Merge origin\n"


def test_another_site_posing_as_this_one_is_refused(client):
    assert client.get("/api/tasks", headers={"Host": "evil.example:5078"}).status_code == 403
    r = client.post("/api/refs/delete", json={"path": "x"}, headers={"Host": "evil.example:5078", "Origin": "http://evil.example:5078"})
    assert r.status_code == 403
    for ok in ["localhost:5078", "127.0.0.1:5078", "192.168.20.5:5001", "[::1]:5078"]:
        assert client.get("/api/tasks", headers={"Host": ok}).status_code == 200, ok


def test_studio_answering_html_is_a_clear_error(client, monkeypatch):
    from upstream_upkeep import studio

    class R:
        status_code, content, text = 200, b"<html>", "<html>"

        def json(self):
            raise ValueError("no json")
    monkeypatch.setattr(studio.requests, "request", lambda *a, **k: R())
    store.save_settings({"studio": {"enabled": True, "url": "http://x"}})
    st = studio.status()
    assert st["ok"] is False and "isn't JSON" in st["error"]
