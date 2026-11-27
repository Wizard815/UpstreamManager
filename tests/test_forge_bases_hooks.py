"""Forge integration beyond push/PRs: a repo as the base for a new one (copy, fork, template),
push webhooks from the forge, and syncing a pull-mirrored forge copy after a push to GitHub.

Live tests run against a real Forgejo when FORGE_TEST_URL and FORGE_TEST_TOKEN are set."""
import hashlib
import hmac
import json
import os
import time
import uuid

import pytest
from conftest import commit_file, git

from upstream_upkeep import app as app_mod, credentials, forge, github, gitops, registry, store, upstream_check

TOKEN = "forgetoken0000000000000000000000000000ab"


@pytest.fixture(autouse=True)
def file_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    for env in credentials.FIELDS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("UPSTREAM_FORGE_HOSTS", raising=False)
    monkeypatch.setattr(credentials, "_file", lambda: tmp_path / "secrets.json")


@pytest.fixture
def home():
    credentials.set("forge_url", "http://forgejo:3000/")
    credentials.set("forge_token", TOKEN)


def _signed(body: dict, secret: str, kind: str = "forgejo") -> tuple[bytes, dict]:
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    headers = {"Content-Type": "application/json", "X-Forgejo-Event": "push"}
    headers.update({"forgejo": {"X-Forgejo-Signature": sig}, "gitea": {"X-Gitea-Signature": sig},
                    "hub": {"X-Hub-Signature-256": "sha256=" + sig}}[kind])
    return raw, headers


def test_verify_hook_takes_every_forge_signature_and_nothing_else():
    for kind in ("forgejo", "gitea", "hub"):
        raw, h = _signed({"a": 1}, "s3cret", kind)
        assert forge.verify_hook(raw, h, "s3cret")
        assert not forge.verify_hook(raw, h, "other")
        assert not forge.verify_hook(raw + b" ", h, "s3cret")
    assert not forge.verify_hook(b"{}", {}, "s3cret") and not forge.verify_hook(b"{}", {"X-Gitea-Signature": "x"}, "")


def test_push_hook_fetches_every_watched_repo_on_that_forge_repo(home, client, repos, monkeypatch):
    _, mine = repos
    git(mine, "remote", "add", "forge", "http://forgejo:3000/Wizard/Mine.git")
    registry.add_repo(str(mine), None, "mine")
    seen = []
    monkeypatch.setattr(upstream_check, "build_merge_plan", lambda path, rid, name, ups, focus_branch=None, fetch=False: seen.append((rid, [u["name"] for u in ups], fetch)))
    monkeypatch.setattr(app_mod.threading, "Thread", lambda target, **kw: type("T", (), {"start": lambda self: target()})())
    credentials.set("forge_webhook_secret", "hooksecret")
    raw, h = _signed({"ref": "refs/heads/main", "repository": {"full_name": "wizard/mine"}}, "hooksecret")
    r = client.post("/api/hooks/forge", data=raw, headers=h)
    assert r.status_code == 202 and r.get_json()["repos"] == ["mine"]
    assert seen == [("mine", ["forge"], True)]
    assert client.post("/api/hooks/forge", data=raw, headers={"Content-Type": "application/json"}).status_code == 401
    other, h2 = _signed({"repository": {"full_name": "someone/else"}}, "hooksecret")
    assert client.post("/api/hooks/forge", data=other, headers=h2).get_json()["repos"] == []


def test_adding_the_webhook_needs_upstreams_address_and_is_done_once(home, client, repos, monkeypatch):
    _, mine = repos
    git(mine, "remote", "add", "forge", "http://forgejo:3000/wizard/mine.git")
    registry.add_repo(str(mine), None, "mine")
    hooks, calls = [], []

    def fake(method, path, token=None, ok404=False, **kw):
        calls.append((method, path, kw.get("json")))
        if method == "GET":
            return hooks
        hooks.append({"id": 9, "config": {"url": kw["json"]["config"]["url"]}, "events": ["push"], "active": True})
        return {"id": 9}
    monkeypatch.setattr(forge, "_api", fake)
    assert "address as the forge reaches it" in client.post("/api/repo/mine/forge/webhook", json={}).get_json()["error"]
    credentials.set("forge_callback_url", "http://upstream-manager:5000/")
    r = client.post("/api/repo/mine/forge/webhook", json={}).get_json()
    assert r["ok"] and r["items"][0]["new"]
    method, path, body = calls[-1]
    assert path == "/repos/wizard/mine/hooks" and body["config"]["url"] == "http://upstream-manager:5000/api/hooks/forge"
    assert body["config"]["secret"] == credentials.get("forge_webhook_secret") and len(body["config"]["secret"]) == 48
    assert body["type"] == "gitea" and "push" in body["events"]
    again = client.post("/api/repo/mine/forge/webhook", json={}).get_json()
    assert again["ok"] and not again["items"][0]["new"] and sum(1 for c in calls if c[0] == "POST") == 1


def test_push_to_github_syncs_the_forge_copy_that_mirrors_it(home, repos, monkeypatch):
    _, mine = repos
    git(mine, "remote", "set-url", "origin", "https://github.com/Wizard815/mine.git")
    git(mine, "remote", "add", "forge", "http://forgejo:3000/wizard/mine.git")
    repo = registry.add_repo(str(mine), None, "mine")
    monkeypatch.setattr(github, "push", lambda *a, **k: {"detail": "pushed", "auth": "token"})
    monkeypatch.setattr(forge, "repo_info", lambda o, r: {"mirror": True, "original_url": "https://github.com/Wizard815/mine"})
    synced = []
    monkeypatch.setattr(forge, "sync", lambda o, r: synced.append(f"{o}/{r}") or {"ok": True})
    out = app_mod._do_push(repo, {"remote": "origin", "branch": "main", "remote_url": "https://github.com/Wizard815/mine.git"})
    assert synced == ["wizard/mine"] and out[1]["ok"] and "fetching it now" in out[1]["detail"]
    # a forge copy that is its own home (not a mirror) is left alone, and so is a push to the forge
    monkeypatch.setattr(forge, "repo_info", lambda o, r: {"mirror": False, "original_url": None})
    assert len(app_mod._do_push(repo, {"remote": "origin", "branch": "main", "remote_url": "https://github.com/Wizard815/mine.git"})) == 1
    assert len(app_mod._do_push(repo, {"remote": "forge", "branch": "main", "remote_url": "http://forgejo:3000/wizard/mine.git"})) == 1


def test_new_repo_refuses_bad_bases_and_modes(home, client):
    assert "mode is copy" in client.post("/api/forge/new", json={"base": "a/b", "mode": "steal"}).get_json()["error"]
    assert "git URL" in client.post("/api/forge/new", json={"base": "--upload-pack=x"}).get_json()["error"]
    assert "needs a base repo on the forge" in client.post("/api/forge/new", json={"base": "https://github.com/o/r", "mode": "fork"}).get_json()["error"]


def test_copy_from_a_github_style_base_keeps_it_as_upstream_and_pushes_home(home, client, repos, monkeypatch, tmp_path):
    up, _ = repos
    git(up, "checkout", "-q", "-b", "feature"); commit_file(up, "f.txt", "x", "feature work"); git(up, "checkout", "-q", "main")
    target = tmp_path / "forge-side.git"
    git(tmp_path, "init", "-q", "--bare", str(target))
    monkeypatch.setattr(forge, "create_repo", lambda name, private=True, description="", org=None: {
        "owner": "wizard", "repo": name, "clone_url": str(target), "web_url": f"http://forgejo:3000/wizard/{name}", "private": private})
    monkeypatch.setattr(registry, "WATCH_FOLDERS", [tmp_path / "watch"])
    r = client.post("/api/forge/new", json={"base": str(up), "mode": "copy", "name": "mything"}).get_json()
    # a plain path isn't a base: only OWNER/REPO or a URL
    assert not r["ok"] and "git URL" in r["error"]
    monkeypatch.setattr(app_mod, "_base_url", lambda base: (str(up), None))
    r = client.post("/api/forge/new", json={"base": "https://github.com/x/up", "mode": "copy", "name": "mything"}).get_json()
    assert r["ok"], r
    repo = registry.get_repo(r["repo"])
    assert repo["home_remote"] == "origin"
    assert {u["name"] for u in repo["upstreams"]} == {"origin", "base"}
    heads = gitops._run(["git", "for-each-ref", "--format=%(refname:strip=2)", "refs/heads"], cwd=str(target)).stdout.split()
    assert sorted(heads) == ["feature", "main"]
    assert gitops.remotes(repo["path"])["base"] == str(up)


# ------------------------------------------------------------------ a real forge
@pytest.fixture
def live_forge(tmp_path, monkeypatch):
    u, t = os.environ.get("FORGE_TEST_URL"), os.environ.get("FORGE_TEST_TOKEN")
    if not (u and t):
        pytest.skip("set FORGE_TEST_URL and FORGE_TEST_TOKEN to run against a real forge")
    credentials.set("forge_url", u)
    credentials.set("forge_token", t)
    monkeypatch.setattr(registry, "WATCH_FOLDERS", [tmp_path / "watch"])
    return u


def _base_on_forge(tmp_path, name):
    made = forge.create_repo(name, private=True)
    src = tmp_path / "src"
    git(tmp_path, "init", "-q", "-b", "main", str(src))
    git(src, "config", "user.email", "t@t.t"); git(src, "config", "user.name", "T")
    commit_file(src, "README.md", "base\n", "base commit")
    git(src, "remote", "add", "origin", made["clone_url"])
    assert gitops._run(["git", "push", "-q", "origin", "main"], cwd=str(src), extra_env=gitops.forge_env(made["clone_url"])).returncode == 0
    return made, src


def test_live_copy_and_template_from_a_forge_base(live_forge, client, tmp_path):
    tag = uuid.uuid4().hex[:6]
    made, src = _base_on_forge(tmp_path, f"base-{tag}")
    r = client.post("/api/forge/new", json={"base": f"{made['owner']}/{made['repo']}", "mode": "copy", "name": f"copy-{tag}"}).get_json()
    assert r["ok"], r
    info = forge.repo_info(made["owner"], f"copy-{tag}")
    assert info and info["private"]
    repo = registry.get_repo(r["repo"])
    assert {u["name"] for u in repo["upstreams"]} == {"origin", "base"}
    # the base moves on: the copy sees it as an upstream change
    commit_file(src, "b.txt", "more\n", "base moves on")
    assert gitops._run(["git", "push", "-q", "origin", "main"], cwd=str(src), extra_env=gitops.forge_env(made["clone_url"])).returncode == 0
    plan = client.post(f"/api/repo/{r['repo']}/fetch", json={}).get_json()["plan"]
    base = next(u for u in plan["upstreams"] if u["name"] == "base")
    assert base["behind"] == 1
    # template: tick Template on the base, then generate
    forge._api("PATCH", f"/repos/{made['owner']}/{made['repo']}", json={"template": True})
    t = client.post("/api/forge/new", json={"base": f"{made['owner']}/{made['repo']}", "mode": "template", "name": f"tmpl-{tag}"}).get_json()
    assert t["ok"], t
    for _ in range(20):   # the forge fills a generated repo in the background
        if gitops._run(["git", "ls-remote", t["made"]["clone_url"]], extra_env=gitops.forge_env(t["made"]["clone_url"])).stdout.strip():
            break
        time.sleep(0.5)
    assert forge.repo_info(made["owner"], f"tmpl-{tag}")["private"]


def test_live_webhook_is_added_once(live_forge, client, repos, tmp_path):
    tag = uuid.uuid4().hex[:6]
    made, _ = _base_on_forge(tmp_path, f"hook-{tag}")
    _, mine = repos
    git(mine, "remote", "add", "forge", made["clone_url"])
    registry.add_repo(str(mine), None, "mine")
    credentials.set("forge_callback_url", "http://upstream-manager:5000")
    r = client.post("/api/repo/mine/forge/webhook", json={}).get_json()
    assert r["ok"] and r["items"][0]["new"], r
    assert [h["url"] for h in forge.hooks(made["owner"], made["repo"])] == ["http://upstream-manager:5000/api/hooks/forge"]
    assert not client.post("/api/repo/mine/forge/webhook", json={}).get_json()["items"][0]["new"]
