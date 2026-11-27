"""The homelab forge (Forgejo/Gitea): remote parsing, token scoping, PRs,
secrets, home remote and mirrors.

The last tests run against a real Forgejo when FORGE_TEST_URL and
FORGE_TEST_TOKEN are set (deploy/forgejo in the Overseer repo, or
`docker run codeberg.org/forgejo/forgejo`); otherwise they are skipped.
"""
import os
import uuid

import pytest
from conftest import commit_file, git

from upstream_upkeep import credentials, forge, github, gitops, registry


@pytest.fixture(autouse=True)
def file_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    for env in credentials.FIELDS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("UPSTREAM_FORGE_HOSTS", raising=False)
    monkeypatch.setattr(credentials, "_file", lambda: tmp_path / "secrets.json")


@pytest.fixture
def home(monkeypatch):
    credentials.set("forge_url", "http://forgejo:3000/")
    credentials.set("forge_token", "forgetoken0000000000000000000000000000ab")
    monkeypatch.setenv("UPSTREAM_FORGE_HOSTS", "192.168.20.5:3030")


# ------------------------------------------------------------------ parsing
@pytest.mark.parametrize("url,expected", [
    ("http://forgejo:3000/wizard/hermes-agent.git", ("forge", "wizard", "hermes-agent")),
    ("http://192.168.20.5:3030/wizard/argos", ("forge", "wizard", "argos")),
    ("ssh://git@192.168.20.5:2224/wizard/argos.git", ("forge", "wizard", "argos")),
    ("git@forgejo:wizard/argos.git", ("forge", "wizard", "argos")),
    ("https://github.com/Wizard815/argos.git", ("github", "Wizard815", "argos")),
    ("http://192.168.20.5:8080/wizard/argos", None),        # same host, another port: not the forge
    ("https://gitlab.com/wizard/argos", None),
    ("http://forgejo:3000/just-one-part", None),
])
def test_parse_knows_the_forge_under_every_address(home, url, expected):
    r = forge.parse(url)
    assert (r and (r.forge, r.owner, r.repo)) == expected


def test_parse_without_a_forge_only_knows_github():
    assert forge.parse("http://forgejo:3000/wizard/argos") is None
    assert forge.parse("https://github.com/a/b").forge == "github"


def test_parse_strips_a_path_prefix(monkeypatch):
    credentials.set("forge_url", "https://lab.example/git")
    assert forge.parse("https://lab.example/git/wizard/argos.git") == forge.Remote("forge", "wizard", "argos")


# ------------------------------------------------------------------ token scoping
def test_forge_token_goes_only_to_forge_hosts(home):
    env = forge.auth_env("tok")
    keys = [env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))]
    assert "http.http://forgejo:3000/.extraheader" in keys
    assert "http.http://192.168.20.5:3030/.extraheader" in keys
    assert not any("github.com" in k for k in keys)
    assert all(env[f"GIT_CONFIG_VALUE_{i}"] == "Authorization: token tok" for i in range(len(keys)))


def _capture_push(monkeypatch, url):
    seen = {}
    monkeypatch.setattr(gitops, "remotes", lambda p: {"origin": url})

    class Done:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(args, cwd=None, check=False, extra_env=None):
        seen.update(args=args, env=extra_env)
        return Done()
    monkeypatch.setattr(gitops, "_run", fake_run)
    return seen


def test_push_to_forge_uses_forge_token_not_github(home, monkeypatch, tmp_path):
    credentials.set("github_token", "github_pat_NOTFORTHEFORGE1")
    seen = _capture_push(monkeypatch, "http://forgejo:3000/wizard/r.git")
    out = github.push(str(tmp_path), "origin", "feature")
    assert out["auth"] == "token"
    assert "NOTFORTHEFORGE" not in repr(seen["env"])
    assert "forgetoken" in repr(seen["env"]) and all("forgetoken" not in a for a in seen["args"])


def test_push_to_github_never_carries_forge_token(home, monkeypatch, tmp_path):
    credentials.set("github_token", "github_pat_GH0000000000001")
    seen = _capture_push(monkeypatch, "https://github.com/o/r.git")
    github.push(str(tmp_path), "origin", "feature")
    assert "forgetoken" not in repr(seen["env"])


def test_push_to_forge_over_ssh_uses_machine_login(home, monkeypatch, tmp_path):
    seen = _capture_push(monkeypatch, "ssh://git@forgejo:2224/wizard/r.git")
    assert github.push(str(tmp_path), "origin", "feature")["auth"] == "machine login"
    assert seen["env"] is None


def test_fetch_and_clone_send_forge_token(home):
    assert gitops.forge_env("http://forgejo:3000/w/r.git")
    assert gitops.forge_env("https://github.com/w/r.git") is None


def test_forge_calls_without_setup_say_what_to_do():
    with pytest.raises(github.GitHubError, match="no forge set up"):
        forge.whoami()


def test_open_pr_and_secrets_dispatch_to_forge(home, monkeypatch):
    calls = []
    monkeypatch.setattr(forge, "_api", lambda method, path, token=None, ok404=False, **kw:
                        calls.append((method, path, kw.get("json"))) or {"html_url": "http://forgejo:3000/w/r/pulls/3", "number": 3})
    pr = github.open_pr("w", "r", "feat", "main", "t", "b", forge="forge")
    assert pr == {"url": "http://forgejo:3000/w/r/pulls/3", "number": 3, "existing": False}
    github.set_secret("w", "r", "DOCKERHUB_TOKEN", "v", forge="forge")
    assert calls[-1] == ("PUT", "/repos/w/r/actions/secrets/DOCKERHUB_TOKEN", {"data": "v"})
    with pytest.raises(github.GitHubError):
        github.set_secret("w", "r", "FORGEJO_TOKEN", "v", forge="forge")


def test_push_mirror_needs_github_url_and_token(home, monkeypatch):
    monkeypatch.setattr(forge, "_api", lambda *a, **k: [])
    with pytest.raises(github.GitHubError, match="github.com repository"):
        forge.add_push_mirror("w", "r", "https://gitlab.com/w/r")
    with pytest.raises(github.GitHubError, match="GitHub token"):
        forge.add_push_mirror("w", "r", "https://github.com/w/r")


# ------------------------------------------------------------------ app: home remote
def test_publish_shows_forge_and_home_remote(home, client, repos):
    _, mine = repos
    git(mine, "remote", "set-url", "origin", "https://github.com/Wizard815/proj.git")
    git(mine, "remote", "add", "forge", "http://forgejo:3000/wizard/proj.git")
    repo = registry.add_repo(str(mine), None, "mine")
    info = client.get(f"/api/repo/{repo['id']}/publish").get_json()
    kinds = {r["name"]: r["forge"] for r in info["remotes"]}
    assert kinds == {"origin": "github", "forge": "forge"}
    assert info["home"] == "origin" and info["owner"] == "Wizard815"
    assert client.post(f"/api/repo/{repo['id']}/home", json={"remote": "forge"}).get_json()["ok"]
    assert client.get(f"/api/repo/{repo['id']}/publish").get_json()["home"] == "forge"
    req = client.post(f"/api/repo/{repo['id']}/push/request", json={"open_pr": True, "base": "dev"}).get_json()["request"]
    assert (req["remote"], req["forge"], req["owner"], req["github_repo"]) == ("forge", "forge", "wizard", "proj")
    assert client.post(f"/api/repo/{repo['id']}/home", json={"remote": "nope"}).status_code == 400


# ------------------------------------------------------------------ against a real Forgejo
LIVE_URL, LIVE_TOKEN = os.environ.get("FORGE_TEST_URL"), os.environ.get("FORGE_TEST_TOKEN")
live = pytest.mark.skipif(not (LIVE_URL and LIVE_TOKEN), reason="set FORGE_TEST_URL and FORGE_TEST_TOKEN to run against a real Forgejo")


@pytest.fixture
def live_forge():
    credentials.set("forge_url", LIVE_URL)
    credentials.set("forge_token", LIVE_TOKEN)


@live
def test_live_create_push_pr_secret_and_mirror(live_forge, client, repos, tmp_path):
    _, mine = repos
    repo = registry.add_repo(str(mine), None, "mine")
    name = f"um-test-{uuid.uuid4().hex[:8]}"
    made = client.post(f"/api/repo/{repo['id']}/forge/create", json={"name": name, "make_home": True}).get_json()
    assert made["ok"], made
    assert made["created"]["private"] is True
    assert gitops.remotes(str(mine))["forge"] == made["created"]["clone_url"]

    # push main, then a branch with a PR, through the forge token
    owner = made["created"]["owner"]
    assert github.push(str(mine), "forge", "main")["auth"] == "token"
    git(mine, "checkout", "-q", "-b", "feature")
    commit_file(mine, "b.txt", "new\n", "add b")
    assert github.push(str(mine), "forge", "feature")["ok"]
    pr = github.open_pr(owner, name, "feature", "main", "Add b", "body", forge="forge")
    again = github.open_pr(owner, name, "feature", "main", "Add b", "body", forge="forge")
    assert again["number"] == pr["number"] and again["existing"]

    # private repo: a clone without the token fails, with it works
    plain = gitops._run(["git", "clone", "-q", made["created"]["clone_url"], str(tmp_path / "anon")])
    assert plain.returncode != 0
    gitops.clone_into(str(tmp_path / "authed"), made["created"]["clone_url"])
    assert (tmp_path / "authed" / "a.txt").exists()
    ok, msg = gitops.fetch(str(mine), "forge", "main")
    assert ok, msg

    github.set_secret(owner, name, "DOCKERHUB_TOKEN", "x", forge="forge")
    assert "DOCKERHUB_TOKEN" in github.secret_names(owner, name, forge="forge")

    # push mirror to GitHub: set up and listed (it can't really push from a test)
    credentials.set("github_token", "github_pat_PLACEHOLDER_NOT_USED")
    assert client.post(f"/api/repo/{repo['id']}/mirrors/push", json={"to": "https://github.com/Wizard815/proj", "sync_on_commit": False}).get_json()["ok"]
    m = client.get(f"/api/repo/{repo['id']}/mirrors").get_json()
    pm = m["remotes"][0]["push_mirrors"]
    assert [p["address"] for p in pm] == ["https://github.com/Wizard815/proj.git"]
    dup = client.post(f"/api/repo/{repo['id']}/mirrors/push", json={"to": "https://github.com/Wizard815/proj"}).get_json()
    assert not dup["ok"] and "already mirrors" in dup["error"]
    assert client.post(f"/api/repo/{repo['id']}/mirrors/push/remove", json={"name": pm[0]["name"]}).get_json()["ok"]
    assert client.get(f"/api/repo/{repo['id']}/mirrors").get_json()["remotes"][0]["push_mirrors"] == []


@live
def test_live_pull_mirror(live_forge):
    """A pull mirror that keeps fetching its source. FORGE_TEST_MIRROR_SOURCE is a
    clone URL the forge itself can reach (a public GitHub repo at home)."""
    src = os.environ.get("FORGE_TEST_MIRROR_SOURCE")
    if not src:
        pytest.skip("set FORGE_TEST_MIRROR_SOURCE to a clone URL the forge can reach")
    name = f"um-mirror-{uuid.uuid4().hex[:8]}"
    m = forge.import_mirror(src, name=name, interval="10m")
    info = forge.repo_info(m["owner"], name)
    assert info["mirror"] and info["private"] and info["original_url"]
    assert forge.sync(m["owner"], name)["ok"]
