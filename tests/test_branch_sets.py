"""Branch sets: several branches merged into the focus branch together, in one workspace."""
from pathlib import Path

import pytest

from upstream_upkeep import registry

from conftest import commit_file, git


@pytest.fixture
def features(repos):
    """main plus three feature branches: a and b touch different files, c conflicts with b."""
    _, mine = repos
    commit_file(mine, "app.py", "x = 1\n", "base app")
    for name, file, text in (("feat-a", "a_feature.py", "a\n"), ("feat-b", "app.py", "x = 2\n"), ("feat-c", "app.py", "x = 3\n")):
        git(mine, "checkout", "-q", "-b", name, "main")
        commit_file(mine, file, text, f"{name} work")
    git(mine, "checkout", "-q", "main")
    registry.add_repo(str(mine), None, "mine")
    return mine


def test_set_merges_in_order_and_stops_at_a_conflict(client, features):
    mine = features
    ws = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-a", "feat-b", "feat-c"]}).get_json()["workspace"]
    assert [s["state"] for s in ws["sources"]] == ["merged", "merged", "conflict"]
    assert ws["unresolved"] == ["app.py"] and not ws["ready"] and "feat-c" in ws["next_step"]
    assert git(mine, "status", "--porcelain") == "" and git(mine, "log", "-1", "--format=%s") == "base app"

    # Continuing with the markers still in the file is refused.
    r = client.post(f"/api/repo/mine/workspaces/{ws['name']}/continue", json={})
    assert r.status_code == 409 and "conflict markers" in r.get_json()["error"]

    # Resolve in the editor (no git commands), then continue.
    save = client.post("/api/repo/mine/file", json={"ws": ws["name"], "path": "app.py", "content": "x = 3  # b and c\n"})
    assert save.get_json()["ok"] and Path(mine, "app.py").read_text() == "x = 1\n"
    ws = client.post(f"/api/repo/mine/workspaces/{ws['name']}/continue", json={}).get_json()["workspace"]
    assert [s["state"] for s in ws["sources"]] == ["merged", "merged", "merged"] and ws["ready"]

    req = client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": "c wins"}).get_json()["request"]
    assert "feat-a, feat-b, feat-c" in req["upstreams"][0]["reason"]
    out = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert out["ok"], out
    assert Path(mine, "app.py").read_text() == "x = 3  # b and c\n" and Path(mine, "a_feature.py").exists()
    log = git(mine, "log", "--format=%s", "-6")
    assert "Merge feat-a" in log and "Merge feat-b" in log and "Merge feat-c" in log


def test_set_pins_each_branch_to_its_commit(client, features):
    mine = features
    ws = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-a"]}).get_json()["workspace"]
    git(mine, "checkout", "-q", "feat-a")
    commit_file(mine, "late.py", "late\n", "after the set was opened")
    git(mine, "checkout", "-q", "main")
    assert ws["ready"]
    req = client.post(f"/api/repo/mine/workspaces/{ws['name']}/submit", json={"summary": ""}).get_json()["request"]
    client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"})
    assert Path(mine, "a_feature.py").exists() and not Path(mine, "late.py").exists()


def test_branch_already_contained_is_skipped(client, features):
    mine = features
    git(mine, "checkout", "-q", "-b", "feat-a2", "feat-a")
    commit_file(mine, "a2.py", "a2\n", "a2 on top of a")
    git(mine, "checkout", "-q", "main")
    ws = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-a2", "feat-a"]}).get_json()["workspace"]
    assert [s["state"] for s in ws["sources"]] == ["merged", "merged"] and ws["ready"]


def test_set_errors(client, features):
    bad = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-a", "nope"]})
    assert bad.status_code == 400 and "nope" in bad.get_json()["error"]
    assert client.post("/api/repo/mine/workspaces", json={"branches": ["main"]}).status_code == 400
    assert client.post("/api/repo/mine/workspaces", json={"branches": ["--output=x"]}).status_code == 400
    assert client.post("/api/repo/mine/workspaces", json={"branches": "feat-a"}).status_code == 400
    assert client.post("/api/repo/mine/workspaces", json={}).status_code == 400


def test_hand_committed_resolution_counts(client, features):
    ws = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-b", "feat-c"]}).get_json()["workspace"]
    Path(ws["path"], "app.py").write_text("x = 23\n")
    git(ws["path"], "add", "app.py")
    git(ws["path"], "-c", "user.email=a@a", "-c", "user.name=A", "commit", "-q", "--no-edit")
    st = client.get(f"/api/repo/mine/workspaces/{ws['name']}").get_json()["workspace"]
    assert [s["state"] for s in st["sources"]] == ["merged", "merged"] and st["ready"]


def test_editor_reads_the_workspace(client, features):
    ws = client.post("/api/repo/mine/workspaces", json={"branches": ["feat-b", "feat-c"]}).get_json()["workspace"]
    f = client.get(f"/api/repo/mine/file?path=app.py&ws={ws['name']}").get_json()
    assert "<<<<<<<" in f["content"]
    ch = client.get(f"/api/repo/mine/changes?ws={ws['name']}").get_json()["items"]
    assert {"path": "app.py", "status": "U"} in ch
    assert client.get("/api/repo/mine/file?path=app.py&ws=merge-nope").status_code == 404
