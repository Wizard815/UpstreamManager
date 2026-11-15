"""Fetching upstreams: only the compared branch, partial clones stay partial, clear errors."""
from pathlib import Path

from upstream_upkeep import gitops, registry

from conftest import commit_file, git


def test_an_upstream_fetches_only_the_compared_branch(client, repos, tmp_path):
    up, mine = repos
    other = tmp_path / "other-up"
    git(tmp_path, "clone", "-q", str(up), str(other))
    git(other, "config", "user.email", "t@t.t"); git(other, "config", "user.name", "T")
    for b in ("feature-a", "feature-b"):
        git(other, "checkout", "-q", "-b", b)
        commit_file(other, f"{b}.txt", "x\n", b)
    git(other, "checkout", "-q", "main")
    commit_file(other, "u.txt", "u\n", "upstream work")
    git(other, "tag", "v9")
    git(mine, "remote", "add", "upstream", str(other))
    registry.add_repo(str(mine), None, "mine")
    res = client.post("/api/repo/mine/fetch", json={}).get_json()
    up_ = next(u for u in res["plan"]["upstreams"] if u["name"] == "upstream")
    assert up_["behind"] == 1
    refs = git(mine, "for-each-ref", "--format=%(refname)", "refs/remotes/upstream").split()
    assert refs == ["refs/remotes/upstream/main"] and "v9" not in git(mine, "tag")


def test_a_partial_clone_stays_partial_and_still_merges(client, tmp_path):
    up = tmp_path / "up"
    up.mkdir()
    git(up, "init", "-q", "-b", "main"); git(up, "config", "user.email", "t@t.t"); git(up, "config", "user.name", "T")
    git(up, "config", "uploadpack.allowFilter", "true"); git(up, "config", "uploadpack.allowAnySHA1InWant", "true")
    commit_file(up, "a.txt", "1\n", "base")
    mine = tmp_path / "mine"
    git(tmp_path, "clone", "-q", "--filter=blob:none", f"file://{up}", str(mine))
    git(mine, "config", "user.email", "t@t.t"); git(mine, "config", "user.name", "T")
    git(mine, "remote", "add", "upstream", f"file://{up}")
    commit_file(up, "big.bin", "B" * 200000, "a big file upstream")
    registry.add_repo(str(mine), None, "mine")
    assert gitops.is_partial(str(mine))
    client.post("/api/repo/mine/fetch", json={})
    blob = git(up, "rev-parse", "HEAD:big.bin")
    missing = git(mine, "rev-list", "--objects", "--missing=print", "refs/remotes/upstream/main")
    assert f"?{blob}" in missing  # history arrived, the big file's contents did not
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    assert res and res[0]["status"] == "merged"
    assert Path(mine, "big.bin").stat().st_size == 200000  # fetched on demand by the merge


def test_a_missing_upstream_branch_names_the_default(client, repos, tmp_path):
    up, mine = repos
    git(up, "branch", "-m", "main", "trunk")
    git(up, "symbolic-ref", "HEAD", "refs/heads/trunk")
    git(mine, "remote", "add", "upstream", str(up))
    registry.add_repo(str(mine), None, "mine")
    res = client.post("/api/run-daily", json={}).get_json()["results"]
    skipped = [r for r in res if r.get("upstream") == "upstream"]
    assert skipped and skipped[0]["status"] == "skipped"
    assert "has no branch main" in skipped[0]["detail"] and "default branch is trunk" in skipped[0]["detail"]
