"""Actions panel: workflow triggers/secrets, fork suggestions, new-workflow flags,
and disabling/enabling through an approval request (GitHub API mocked)."""

import pytest
from conftest import commit_file, git

from upstream_upkeep import actions, credentials, github, registry

CI = "name: CI\non:\n  pull_request:\n  push:\n    branches: [main]\njobs:\n  t:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ${{ secrets.GITHUB_TOKEN }}\n"
DEPLOY = ("name: Deploy Backend to GKE\non:\n  schedule:\n    - cron: '0 3 * * *'\n  workflow_dispatch:\njobs:\n  d:\n    runs-on: ubuntu-latest\n"
          "    steps:\n      - run: deploy --key ${{ secrets.GCP_SA_KEY }} --t ${{ secrets.SLACK_TOKEN }}\n")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    for env in credentials.FIELDS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(credentials, "_file", lambda: tmp_path / "secrets.json")
    actions.clear_cache()


@pytest.fixture
def fork(repos):
    """`mine` with two workflows and an upstream that adds a third. Returns (up, mine)."""
    up, mine = repos
    (up / ".github/workflows").mkdir(parents=True)
    for name, text in (("ci.yml", CI), ("deploy.yml", DEPLOY)):
        commit_file(up, f".github/workflows/{name}", text, f"add {name}")
    git(mine, "pull", "-q", "origin", "main")
    git(mine, "remote", "set-url", "origin", "https://github.com/me/proj.git")
    git(mine, "remote", "add", "upstream", str(up))
    commit_file(up, ".github/workflows/nightly.yml", "name: Nightly\non:\n  schedule:\n    - cron: '0 0 * * *'\njobs: {}\n", "add nightly")
    git(mine, "fetch", "-q", "upstream")
    registry.add_repo(str(mine), None, "mine")
    return up, mine


def fake_github(monkeypatch, fork=True, states=None):
    states = states or {1: "active", 2: "active"}

    def get(path, params=None):
        if path == "/repos/me/proj":
            return {"fork": fork, "parent": {"full_name": "big/proj"} if fork else None}
        if path.endswith("/actions/workflows"):
            return {"workflows": [{"id": 1, "name": "CI", "path": ".github/workflows/ci.yml", "state": states[1]},
                                  {"id": 2, "name": "Deploy Backend to GKE", "path": ".github/workflows/deploy.yml", "state": states[2]},
                                  {"id": 3, "name": "Dependabot", "path": "dynamic/dependabot/x", "state": "active"}]}
        if path.endswith("/actions/runs"):
            runs = [{"workflow_id": 2, "conclusion": "failure", "created_at": "2026-10-20"}] * 5
            runs += [{"workflow_id": 1, "conclusion": "success", "created_at": "2026-10-20"}] * 3
            return {"workflow_runs": runs}
        raise AssertionError(path)
    monkeypatch.setattr(actions, "_get", get)


def test_file_info_reads_triggers_and_secrets(fork):
    _, mine = fork
    info = actions.file_info(str(mine), ".github/workflows/deploy.yml")
    assert info["schedule"] and set(info["triggers"]) == {"schedule", "workflow_dispatch"}
    assert info["secrets"] == ["GCP_SA_KEY", "SLACK_TOKEN"]  # GITHUB_TOKEN never counts
    assert actions.file_info(str(mine), ".github/workflows/ci.yml")["secrets"] == []


def test_fork_suggestions_and_flags(client, fork, monkeypatch):
    _, mine = fork
    fake_github(monkeypatch)
    data = client.get("/api/repo/mine/actions").get_json()
    assert data["ok"] and data["fork"] and data["parent"] == "big/proj"
    by = {w["name"]: w for w in data["workflows"]}
    assert "Dependabot" not in by  # dynamic workflows have no file
    assert by["CI"]["reasons"] == [] and by["CI"]["stats"]["succeeded"] == 3
    reasons = " ".join(by["Deploy Backend to GKE"]["reasons"])
    assert "failed 5 of its last 5" in reasons and "schedule" in reasons and "release/deploy" in reasons and "GCP_SA_KEY" in reasons
    assert data["summary"]["suggested"] == 1
    assert [w["path"] for w in data["incoming"]] == [".github/workflows/nightly.yml"]
    assert data["incoming"][0]["schedule"]


def test_not_a_fork_only_flags_failures(client, fork, monkeypatch):
    fake_github(monkeypatch, fork=False)
    by = {w["name"]: w for w in client.get("/api/repo/mine/actions").get_json()["workflows"]}
    assert by["Deploy Backend to GKE"]["reasons"] == ["failed 5 of its last 5 runs"]


def test_new_since_review_after_a_pull(client, fork, monkeypatch):
    up, mine = fork
    fake_github(monkeypatch)
    client.get("/api/repo/mine/actions")  # first look sets the baseline
    git(mine, "pull", "-q", "--no-rebase", "upstream", "main")  # brings nightly.yml
    actions.clear_cache()
    orig = actions._get

    def with_nightly(path, params=None):
        d = orig(path, params)
        if path.endswith("/actions/workflows"):
            d = {"workflows": d["workflows"] + [{"id": 4, "name": "Nightly", "path": ".github/workflows/nightly.yml", "state": "active"}]}
        return d
    monkeypatch.setattr(actions, "_get", with_nightly)
    by = {w["name"]: w for w in client.get("/api/repo/mine/actions").get_json()["workflows"]}
    assert by["Nightly"]["new_since_review"] and not by["CI"]["new_since_review"]
    client.post("/api/repo/mine/actions/reviewed", json={})
    by = {w["name"]: w for w in client.get("/api/repo/mine/actions").get_json()["workflows"]}
    assert not by["Nightly"]["new_since_review"]


def test_disable_goes_through_approval(client, fork, monkeypatch):
    fake_github(monkeypatch)
    calls = []
    monkeypatch.setattr(github, "_api", lambda method, path, token=None, **kw: calls.append((method, path)) or {})
    credentials.set("github_token", "github_pat_ACTIONS0000001")
    req = client.post("/api/repo/mine/actions/request", json={"disable": [2, 1]}).get_json()["request"]
    assert sorted(w["name"] for w in req["disable"]) == ["CI", "Deploy Backend to GKE"]
    assert [c for c in calls if c[0] != "GET"] == []  # nothing changes before approval (reads are fine)
    body = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert body["ok"], body
    assert sorted(c for c in calls if c[0] == "PUT") == [("PUT", "/repos/me/proj/actions/workflows/1/disable"),
                                                         ("PUT", "/repos/me/proj/actions/workflows/2/disable")]


def test_enable_only_picks_disabled_and_no_token_fails_cleanly(client, fork, monkeypatch):
    fake_github(monkeypatch, states={1: "active", 2: "disabled_manually"})
    r = client.post("/api/repo/mine/actions/request", json={"enable": [1, 2]}).get_json()
    assert [w["id"] for w in r["request"]["enable"]] == [2]  # CI is already active
    body = client.post(f"/api/request/{r['request']['id']}/decide", json={"decision": "approve"}).get_json()
    assert not body["ok"] and "no GitHub token" in body["request"]["result"][0]["detail"]
