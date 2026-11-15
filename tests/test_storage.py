"""Disk use and cleanup of Upstream Manager's own data; components in the file tree."""
from datetime import datetime, timedelta, timezone

from upstream_upkeep import config, registry, storage, store

from conftest import commit_file, git


def test_storage_reports_each_repo_and_its_location(client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    d = client.get("/api/storage").get_json()
    assert d["ok"] and d["data_dir"] == str(config.CONFIG_PATH.parent)
    (r,) = d["repos"]
    assert r["path"] == str(mine) and r["git"] > 0 and r["component_copies"] == 0 and r["partial"] is False


def test_cleanup_removes_only_old_decided_requests_and_unused_copies(client, repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    old = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
    new = datetime.now(timezone.utc).isoformat()
    for rid, status, when in [("old", "approved", old), ("recent", "denied", new), ("waiting", "pending", None)]:
        store.record_request({"id": rid, "repo_id": repo["id"], "status": status, "decided_at": when})
    stale = mine / ".git" / "upkeep-components" / "gone.git"
    (stale / "objects").mkdir(parents=True)
    (stale / "objects" / "x").write_bytes(b"0" * 1000)
    orphan_ws = config.CONFIG_PATH.parent / "workspaces" / "removed-repo"
    orphan_ws.mkdir(parents=True)

    preview = client.post("/api/storage/cleanup", json={"dry_run": True}).get_json()
    assert preview["requests"] == ["old"] and [c["name"] for c in preview["component_copies"]] == ["gone"]
    assert [w["folder"] for w in preview["workspaces"]] == ["removed-repo"] and stale.exists()

    # an agent may look but not clean
    r = client.post("/api/storage/cleanup", json={}, headers={"X-Upstream-Client": "agent"})
    assert r.status_code == 403 and stale.exists()

    done = client.post("/api/storage/cleanup", json={}).get_json()
    assert done["freed"] >= 1000 and not stale.exists() and not orphan_ws.exists()
    assert {x["id"] for x in store.get_requests()} == {"recent", "waiting"}
    assert git(mine, "status", "--porcelain") == ""  # the repo itself is untouched


def test_a_component_copy_in_use_is_kept(repos):
    up, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    repo["components"] = [{"name": "lib", "path": "lib", "url": str(up), "branch": "main"}]
    keep = mine / ".git" / "upkeep-components" / "lib.git"
    keep.mkdir(parents=True)
    stored = next(r for r in store.all_repos() if r["id"] == repo["id"])
    stored["components"] = repo["components"]
    assert storage.cleanup(dry_run=True)["component_copies"] == []
    storage.cleanup()
    assert keep.exists()


def test_tree_marks_components_and_explains_an_empty_one(client, tmp_path):
    lib, hub = tmp_path / "lib", tmp_path / "hub"
    for r in (lib, hub):
        r.mkdir()
        git(r, "init", "-q", "-b", "main")
        git(r, "config", "user.email", "t@t.t")
        git(r, "config", "user.name", "T")
    commit_file(lib, "l.txt", "l\n", "lib")
    commit_file(hub, "h.txt", "h\n", "hub")
    git(hub, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(lib), "vendor/lib")
    git(hub, "commit", "-qm", "add lib")
    repo = registry.add_repo(str(hub), None, "hub")
    vendor = client.get(f"/api/repo/{repo['id']}/tree?path=vendor").get_json()
    (e,) = vendor["entries"]
    assert e["submodule"] and e["checked_out"]
    git(hub, "submodule", "deinit", "-f", "vendor/lib")
    empty = client.get(f"/api/repo/{repo['id']}/tree?path=vendor/lib").get_json()
    assert empty["entries"] == [] and "not checked out" in empty["note"]
    assert client.get(f"/api/repo/{repo['id']}/tree?path=vendor").get_json()["entries"][0]["checked_out"] is False


import pytest  # noqa: E402

from upstream_upkeep.registry import parse_git_url, repo_dir_for_url  # noqa: E402


@pytest.mark.parametrize("url,folder", [
    ("git@gitlab.com:group/proj.git", "proj"),
    ("https://gitea.example.lan:3000/me/thing", "thing"),
    ("ssh://git@codeberg.org/me/x.git", "x"),
    ("https://dev.azure.com/org/proj/_git/repo", "repo"),
    ("file:///srv/git/m.git", "m"),
])
def test_third_party_hosts_are_kept_as_given(url, folder, tmp_path):
    assert parse_git_url(url) == url
    assert repo_dir_for_url(tmp_path, url) == str(tmp_path / folder)


@pytest.mark.parametrize("url", ["not a url", "--upload-pack=touch x", "ext::sh -c x", "/no/such/dir", "javascript:x"])
def test_odd_urls_are_refused(url):
    with pytest.raises(ValueError):
        parse_git_url(url)


def test_a_local_mirror_folder_is_a_url(tmp_path):
    assert parse_git_url(str(tmp_path)) == str(tmp_path)
