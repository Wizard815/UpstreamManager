"""The reference library: one copy linked into many folders, the blanket update, edited references,
the ManagerFiles layout, and herdr's default agent."""
import os
from pathlib import Path

import pytest

from upstream_upkeep import config, herdr, references, store

from conftest import commit_file, git


@pytest.fixture
def lib(tmp_path):
    store.save_settings({"references": {"root": str(tmp_path / "refs")}})
    (tmp_path / "refs").mkdir()
    return tmp_path / "refs"


def clone_ref(lib, upr, rel):
    dest = lib / rel
    dest.parent.mkdir(parents=True, exist_ok=True)
    git(lib, "clone", "-q", str(upr), str(dest))
    git(dest, "config", "user.email", "t@t.t")
    git(dest, "config", "user.name", "T")
    return dest


# ------------------------------------------------------------ links
def test_link_shows_one_copy_in_two_folders(client, lib, repos):
    upr, _ = repos
    clone_ref(lib, upr, "shared/upr")
    for f in ("allglance", "therock"):
        assert client.post("/api/refs/folder", json={"parent": "", "name": f}).get_json()["ok"]
        r = client.post("/api/refs/link", json={"target": "shared/upr", "into": f}).get_json()
        assert r["ok"] and r["link_to"] == "shared/upr", r
    item = [i for i in client.get("/api/refs?path=allglance").get_json()["items"] if i["name"] == "upr"][0]
    assert item["kind"] == "repo" and item["link_to"] == "shared/upr"
    assert "one" in client.get("/api/refs/read?path=allglance/upr/a.txt").get_json()["text"]
    assert references.all_repos() == ["shared/upr"]           # counted (and updated) once
    assert sorted(client.get("/api/refs/links?path=shared/upr").get_json()["items"]) == ["allglance/upr", "therock/upr"]
    # the link is relative, so the library can move as a whole
    assert not os.path.isabs(os.readlink(lib / "allglance" / "upr"))


def test_link_to_a_link_points_at_the_real_thing(client, lib, repos):
    clone_ref(lib, repos[0], "shared/upr")
    (lib / "a").mkdir(); (lib / "b").mkdir()
    references.link("shared/upr", "a")
    assert references.link("a/upr", "b")["link_to"] == "shared/upr"


def test_link_refusals(client, lib, repos):
    clone_ref(lib, repos[0], "shared/upr")
    (lib / "a").mkdir()
    references.link("shared/upr", "a")
    bad = [{"target": "shared/upr", "into": "a"},                 # already there
           {"target": "shared", "into": "shared"},                # into itself
           {"target": "shared", "into": "shared/upr"},            # into a clone
           {"target": "../etc", "into": "a"}, {"target": "nope", "into": "a"}]
    for body in bad:
        assert client.post("/api/refs/link", json=body).status_code in (400, 404), body


def test_moving_the_original_keeps_links_working(client, lib, repos):
    clone_ref(lib, repos[0], "shared/upr")
    (lib / "a").mkdir(); (lib / "keep").mkdir()
    references.link("shared/upr", "a")
    r = client.post("/api/refs/move", json={"path": "shared/upr", "to": "keep"}).get_json()
    assert r["ok"] and r["relinked"] == ["a/upr"]
    assert (lib / "a" / "upr" / "a.txt").is_file()
    # moving a link moves only the link
    (lib / "b").mkdir()
    assert client.post("/api/refs/move", json={"path": "a/upr", "to": "b"}).get_json()["ok"]
    assert (lib / "b" / "upr").is_symlink() and (lib / "b" / "upr" / "a.txt").is_file() and (lib / "keep" / "upr").is_dir()


def test_deleting_a_link_keeps_the_original_and_deleting_the_original_removes_links(client, lib, repos):
    clone_ref(lib, repos[0], "shared/upr")
    (lib / "a").mkdir(); (lib / "b").mkdir()
    references.link("shared/upr", "a"); references.link("shared/upr", "b")
    assert client.post("/api/refs/delete", json={"path": "a/upr"}).get_json()["ok"]
    assert (lib / "shared" / "upr").is_dir() and not (lib / "a" / "upr").exists()
    r = client.post("/api/refs/delete", json={"path": "shared/upr"}).get_json()
    assert r["removed_links"] == ["b/upr"] and not os.path.lexists(lib / "b" / "upr")


def test_cloning_the_same_repo_again_links_instead(client, lib, repos):
    upr, _ = repos
    clone_ref(lib, upr, "shared/upr")
    (lib / "p").mkdir()
    r = client.post("/api/refs/clone", json={"url": f"file://{upr}", "folder": "p"}).get_json()
    assert r["ok"] and r["linked"] and r["link_to"] == "shared/upr"
    assert r["path"] == "p/up" and (lib / "p" / "up").is_symlink()   # named like a fresh clone would be


@pytest.mark.parametrize("a,b", [("https://github.com/Org/Repo.git", "git@github.com:org/repo"),
                                 ("https://github.com/org/repo/", "ssh://git@github.com/org/repo.git")])
def test_same_url_spellings(a, b):
    assert references._same_url(a) == references._same_url(b)


def test_a_link_leading_outside_is_not_followed(client, lib, tmp_path):
    (tmp_path / "secret.txt").write_text("no")
    os.symlink(tmp_path / "secret.txt", lib / "out.txt")
    assert client.get("/api/refs/read?path=out.txt").status_code == 400
    assert client.get("/api/refs/raw?path=out.txt").status_code == 404


# ------------------------------------------------------------ updates and edits
def test_update_all_fast_forwards_untouched_and_only_fetches_edited(client, lib, repos):
    upr, _ = repos
    clean = clone_ref(lib, upr, "clean")
    mine = clone_ref(lib, upr, "mine")
    commit_file(mine, "mine.txt", "x", "my own commit")
    commit_file(upr, "new.txt", "new", "upstream moved")
    r = references.update_all(wait=True)
    assert r["state"] == "finished"
    st = client.get("/api/refs/update-all").get_json()
    assert st["state"] == "finished" and st["done"] == ["clean"] and st["edited"] == ["mine"] and not st["failed"], st
    assert (clean / "new.txt").is_file()
    assert not (mine / "new.txt").exists() and (mine / "mine.txt").is_file()        # yours is never changed
    assert git(mine, "rev-parse", "origin/main") == git(upr, "rev-parse", "HEAD")   # but it was fetched
    assert st["last"]


def test_update_all_carries_on_past_a_broken_clone(client, lib, repos):
    clone_ref(lib, repos[0], "good")
    bad = clone_ref(lib, repos[0], "bad")
    git(bad, "remote", "set-url", "origin", str(lib / "nowhere"))
    references.update_all(wait=True)
    st = references.update_all_status()
    assert st["done"] == ["good"] and [f["path"] for f in st["failed"]] == ["bad"]


def test_edited_references_and_their_changes_reach_the_model(client, lib, repos):
    ref = clone_ref(lib, repos[0], "r")
    assert client.get("/api/refs/edited").get_json()["items"] == []
    (ref / "a.txt").write_text("changed by me\n")
    items = client.get("/api/refs/edited").get_json()["items"]
    assert [i["path"] for i in items] == ["r"] and items[0]["files"] == ["a.txt"]
    text, _ = references.for_model("r")
    assert "Edited by the user" in text and "changed by me" in text
    tree_item = client.get("/api/refs").get_json()["items"][0]
    assert tree_item["edited"]["files_count"] == 1
    # a single update of an edited one only fetches
    r = client.post("/api/refs/update", json={"path": "r"}).get_json()
    assert r["ok"] and r["skipped"]


def test_auto_update_setting(client, lib, monkeypatch):
    monkeypatch.delenv("UPSTREAM_REFS_UPDATE_HOURS", raising=False)
    assert references.auto_update_hours() == 24
    assert client.post("/api/refs/auto-update", json={"hours": 0}).get_json()["every_hours"] == 0
    assert references.auto_update_hours() == 0
    assert client.post("/api/refs/auto-update", json={"hours": "x"}).status_code == 400
    assert client.post("/api/refs/auto-update", json={"hours": 6}, headers={"X-Upstream-Client": "agent"}).status_code == 403


# ------------------------------------------------------------ ManagerFiles layout
def test_manager_home_layout(monkeypatch, tmp_path):
    for k in ("UPSTREAM_HOME", "UPSTREAM_REPOS_DIR", "REPOS_DIR", "UPSTREAM_REFERENCES_DIR"):
        monkeypatch.delenv(k, raising=False)
    assert config.repos_dir() is None and config.references_dir() == config.CONFIG_PATH.parent / "references"
    monkeypatch.setenv("UPSTREAM_HOME", str(tmp_path / "ManagerFiles"))
    assert config.repos_dir() == tmp_path / "ManagerFiles" / "Repos"
    assert config.references_dir() == tmp_path / "ManagerFiles" / "References"
    monkeypatch.setenv("UPSTREAM_REFERENCES_DIR", str(tmp_path / "elsewhere"))
    assert config.references_dir() == tmp_path / "elsewhere"
    monkeypatch.delenv("UPSTREAM_HOME"); monkeypatch.delenv("UPSTREAM_REFERENCES_DIR")
    monkeypatch.setenv("REPOS_DIR", str(tmp_path / "code" / "repos"))
    assert config.references_dir() == tmp_path / "code" / "References"   # next to where the repos are


def test_library_defaults_to_manager_home_unless_settings_say_otherwise(monkeypatch, tmp_path):
    store.save_settings({"references": {}})
    monkeypatch.setenv("UPSTREAM_HOME", str(tmp_path / "MF"))
    monkeypatch.delenv("UPSTREAM_REFERENCES_DIR", raising=False)
    assert references.root() == tmp_path / "MF" / "References"
    store.save_settings({"references": {"root": str(tmp_path / "picked")}})
    assert references.root() == tmp_path / "picked"


# ------------------------------------------------------------ herdr
def test_herdr_starts_hermes_by_default_and_lists_installed_first(monkeypatch, tmp_path):
    monkeypatch.delenv("UPSTREAM_HERDR_KIND", raising=False)
    store.save_settings({"herdr": {}})
    assert herdr.settings()["kind"] == "hermes"
    bin_ = tmp_path / "bin"; bin_.mkdir()
    for name in ("codex", "cursor-agent"):
        p = bin_ / name; p.write_text("#!/bin/sh\n"); p.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_))
    st = herdr.status()
    assert st["installed"] == ["codex", "cursor"]
    assert st["kinds"][:3] == ["hermes", "codex", "cursor"] and len(st["kinds"]) == len(set(st["kinds"]))
