"""Submodule components: detection, per-component compare, one-commit bumps,
watched paths, chat facts and the memory relevance filter."""
from pathlib import Path

import pytest
from conftest import commit_file, git

from upstream_upkeep import components, llm, registry, store


def _app(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@t.t")
    git(repo, "config", "user.name", "T")
    (repo / "src").mkdir()
    commit_file(repo, "src/model.js", "export const v = 1\n", "model v1")
    commit_file(repo, "README.md", "app\n", "readme")
    return repo


@pytest.fixture
def hub(tmp_path):
    """A hub repo with two submodules (apps/a, apps/b). Returns (hub, app_a, app_b)."""
    a, b = _app(tmp_path, "app-a"), _app(tmp_path, "app-b")
    hub = tmp_path / "hub"
    hub.mkdir()
    git(hub, "init", "-q", "-b", "main")
    git(hub, "config", "user.email", "t@t.t")
    git(hub, "config", "user.name", "T")
    commit_file(hub, "gateway.js", "// hub code\n", "hub code")
    for name, src in (("a", a), ("b", b)):
        git(hub, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(src), f"apps/{name}")
    git(hub, "commit", "-qm", "add apps")
    registry.add_repo(str(hub), None, "hub")
    return hub, a, b


def _pins(hub):
    return {p: git(hub, "ls-tree", "HEAD", p).split()[2] for p in ("apps/a", "apps/b")}


def test_detect_and_sync_keep_settings(hub):
    h, *_ = hub
    repo = store.get_repo("hub")
    assert [c["name"] for c in repo["components"]] == ["apps/a", "apps/b"]
    components.update_settings(repo["components"][0], {"watch_paths": "src/, docs", "after_update": "node vendor.mjs"})
    again = components.sync(repo)
    assert again[0]["watch_paths"] == ["src", "docs"] and again[0]["after_update"] == "node vendor.mjs"


def test_settings_route_accepts_path_names(client, hub):
    for url in ("/api/repo/hub/components/apps/a", "/api/repo/hub/components/apps%2Fa"):
        r = client.post(url, json={"watch_paths": "src", "after_update": "make vendor"})
        assert r.status_code == 200 and r.get_json()["item"]["watch_paths"] == ["src"], url
    assert store.get_repo("hub")["components"][0]["after_update"] == "make vendor"


def test_compare_counts_inside_the_submodule(hub):
    h, a, _ = hub
    commit_file(a, "src/model.js", "export const v = 2\n", "model v2")
    commit_file(a, "README.md", "app 2\n", "readme 2")
    repo = store.get_repo("hub")
    components.update_settings(repo["components"][0], {"watch_paths": ["src"]})
    st = components.compare(str(h), repo["components"][0], do_fetch=True)
    assert (st["behind"], st["local"], st["status"]) == (2, 0, "ok")
    assert st["watched_changes"] == ["src/model.js"]
    assert [m["subject"] for m in st["missing"]] == ["readme 2", "model v2"]
    # The upstream's objects live in the submodule's git dir, not the hub's.
    upstream = git(a, "rev-parse", "HEAD")
    assert git(h / "apps/a", "cat-file", "-t", upstream) == "commit"
    with pytest.raises(Exception):
        git(h, "cat-file", "-t", upstream)


def test_approval_bumps_all_pins_in_one_commit(client, hub):
    h, a, b = hub
    commit_file(a, "src/model.js", "v2\n", "a2")
    commit_file(b, "README.md", "b2\n", "b2")
    repo = store.get_repo("hub")
    components.update_settings(repo["components"][0], {"watch_paths": ["src"], "after_update": "node vendor.mjs"})
    assert client.post("/api/repo/hub/components/fetch", json={}).get_json()["ok"]
    req = client.post("/api/repo/hub/components/request", json={}).get_json()["request"]
    assert [c["name"] for c in req["components"]] == ["apps/a", "apps/b"]
    head_before = git(h, "rev-parse", "HEAD")
    body = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert body["ok"], body
    assert git(h, "rev-list", "--count", f"{head_before}..HEAD") == "1"
    changed = git(h, "diff", "--name-only", head_before, "HEAD").splitlines()
    assert sorted(changed) == ["apps/a", "apps/b"]
    assert _pins(h) == {"apps/a": git(a, "rev-parse", "HEAD"), "apps/b": git(b, "rev-parse", "HEAD")}
    assert "Bump apps/a" in git(h, "log", "-1", "--format=%s")
    hints = [o for o in body["request"]["result"] if o.get("after_update")]
    assert hints and hints[0]["after_update"] == "node vendor.mjs"
    assert git(h, "status", "--porcelain") == ""


def test_request_for_one_component_bumps_only_that_one(client, hub):
    h, a, b = hub
    commit_file(a, "README.md", "a2\n", "a2")
    commit_file(b, "README.md", "b2\n", "b2")
    client.post("/api/repo/hub/components/fetch", json={})
    pins = _pins(h)
    req = client.post("/api/repo/hub/components/request", json={"names": ["apps/b"]}).get_json()["request"]
    assert [c["name"] for c in req["components"]] == ["apps/b"]
    assert client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()["ok"]
    after = _pins(h)
    assert after["apps/a"] == pins["apps/a"] and after["apps/b"] == git(b, "rev-parse", "HEAD")


def test_component_with_own_commits_is_skipped(hub):
    h, a, b = hub
    sub = h / "apps/a"
    git(sub, "config", "user.email", "t@t.t")
    git(sub, "config", "user.name", "T")
    git(sub, "checkout", "-q", "-b", "local")
    commit_file(sub, "patch.txt", "mine\n", "my patch")
    git(h, "commit", "-qam", "pin my patch")
    commit_file(a, "README.md", "a2\n", "a2")
    commit_file(b, "README.md", "b2\n", "b2")
    repo = store.get_repo("hub")
    for c in repo["components"]:
        components.fetch(str(h), c)
    out = components.bump(repo, ["apps/a", "apps/b"], "main")
    by = {r["name"]: r for r in out["results"]}
    assert not by["apps/a"]["ok"] and "not upstream" in by["apps/a"]["detail"]
    assert by["apps/b"]["ok"] and out["commit"]


def test_staged_change_blocks_and_failed_commit_rolls_back(hub):
    h, a, _ = hub
    commit_file(a, "README.md", "a2\n", "a2")
    repo = store.get_repo("hub")
    components.fetch(str(h), repo["components"][0])
    pins = _pins(h)
    (h / "gateway.js").write_text("// edited\n")
    git(h, "add", "gateway.js")
    out = components.bump(repo, ["apps/a"], "main")
    assert not out["ok"] and "staged" in out["detail"] and _pins(h) == pins
    git(h, "reset", "-q", "--hard")
    # A failing commit (pre-commit hook) must leave the submodule on its old pin.
    hook = h / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    out = components.bump(repo, ["apps/a"], "main")
    assert not out["ok"] and "rolled back" in out["detail"]
    assert git(h / "apps/a", "rev-parse", "HEAD") == pins["apps/a"]
    assert git(h, "status", "--porcelain") == ""


def test_daily_check_raises_one_request_and_never_bumps(client, hub):
    h, a, b = hub
    commit_file(a, "README.md", "a2\n", "a2")
    commit_file(b, "README.md", "b2\n", "b2")
    pins = _pins(h)
    for _ in range(2):
        client.post("/api/run-daily", json={})
    reqs = [r for r in client.get("/api/requests").get_json() if r.get("type") == "components"]
    assert len(reqs) == 1 and len(reqs[0]["components"]) == 2
    assert _pins(h) == pins


def test_chat_facts_list_components(hub):
    h, a, _ = hub
    commit_file(a, "src/model.js", "v2\n", "a2")
    repo = store.get_repo("hub")
    components.update_settings(repo["components"][0], {"watch_paths": ["src"], "after_update": "node vendor.mjs"})
    components.fetch(str(h), repo["components"][0])
    facts = "\n".join(llm._component_facts(repo))
    assert "exactly 2 (apps/a, apps/b)" in facts
    assert "1 commit(s) BEHIND upstream" in facts and "NOT up to date" in facts
    assert "run `node vendor.mjs`" in facts
    assert "  apps/b <- " in facts


def test_cross_repo_memory_needs_a_strong_match():
    assert not llm._relevant({"score": 0.62}, llm.OTHER_REPO_MIN_SCORE)
    assert llm._relevant({"score": 0.85}, llm.OTHER_REPO_MIN_SCORE)
    assert llm._relevant({"score": None}, llm.OTHER_REPO_MIN_SCORE)
