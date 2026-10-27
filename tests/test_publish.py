"""Accounts, token pushes, PRs, Actions secrets and the Docker workflow builder."""
import base64
from pathlib import Path

import pytest
import yaml
from conftest import commit_file, git

from upstream_upkeep import credentials, github, gitops, registry, store, workflows


@pytest.fixture(autouse=True)
def file_secrets(monkeypatch, tmp_path):
    """Keep tests off the real OS keychain and away from real env tokens."""
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    for env in credentials.FIELDS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(credentials, "_file", lambda: tmp_path / "secrets.json")


# ------------------------------------------------------------------ credentials
def test_credentials_store_mask_and_env_override(client, monkeypatch):
    token = "github_pat_ABCDEFGHIJKL1234"
    r = client.post("/api/credentials", json={"github_token": token, "dockerhub_username": "wiz"}).get_json()
    assert r["stored"]["github_token"] == "file"
    resp = client.get("/api/credentials")
    assert token not in resp.get_data(as_text=True)
    body = resp.get_json()
    assert body["github_token"]["hint"] == "••••1234" and body["dockerhub_username"]["hint"] == "wiz"
    assert credentials.get("github_token") == token
    monkeypatch.setenv("UPSTREAM_GITHUB_TOKEN", "from-env-token-9999")
    assert credentials.get("github_token") == "from-env-token-9999"
    assert credentials.public()["github_token"]["source"] == "environment"
    monkeypatch.delenv("UPSTREAM_GITHUB_TOKEN")
    client.post("/api/credentials", json={"github_token": ""})
    assert credentials.get("github_token") == ""


def test_state_file_never_holds_secrets(client):
    client.post("/api/credentials", json={"github_token": "github_pat_SECRETSECRET0000"})
    store.save_state()
    assert "SECRETSECRET" not in Path(store.config.CONFIG_PATH).read_text(encoding="utf-8") if Path(store.config.CONFIG_PATH).exists() else True


# ------------------------------------------------------------------ github
@pytest.mark.parametrize("url,expected", [
    ("https://github.com/Wizard815/ALLGlance", ("Wizard815", "ALLGlance")),
    ("https://github.com/Wizard815/ALLGlance.git", ("Wizard815", "ALLGlance")),
    ("git@github.com:Wizard815/hermes-agent.git", ("Wizard815", "hermes-agent")),
    ("https://gitlab.com/a/b", None),
])
def test_parse_remote(url, expected):
    assert github.parse_remote(url) == expected


def test_token_goes_in_env_header_never_in_args(monkeypatch, tmp_path):
    credentials.set("github_token", "github_pat_TOKEN123456789")
    seen = {}
    monkeypatch.setattr(gitops, "remotes", lambda p: {"origin": "https://github.com/o/r.git"})

    class Done:
        returncode, stdout, stderr = 0, "ok", ""

    def fake_run(args, cwd=None, check=False, extra_env=None):
        seen.update(args=args, env=extra_env)
        return Done()
    monkeypatch.setattr(gitops, "_run", fake_run)
    out = github.push(str(tmp_path), "origin", "feature")
    assert out["auth"] == "token"
    assert all("TOKEN123" not in a for a in seen["args"])
    assert seen["env"]["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    assert base64.b64decode(seen["env"]["GIT_CONFIG_VALUE_0"].split()[-1]).decode() == "x-access-token:github_pat_TOKEN123456789"
    assert "--force" not in seen["args"] and "-f" not in seen["args"]


def test_token_not_sent_to_other_hosts(monkeypatch, tmp_path):
    credentials.set("github_token", "github_pat_TOKEN123456789")
    monkeypatch.setattr(gitops, "remotes", lambda p: {"origin": "https://gitlab.com/o/r.git"})
    seen = {}

    class Done:
        returncode, stdout, stderr = 0, "", ""
    monkeypatch.setattr(gitops, "_run", lambda args, cwd=None, check=False, extra_env=None: (seen.update(env=extra_env), Done())[1])
    assert github.push(str(tmp_path), "origin", "main")["auth"] == "machine login" and seen["env"] is None


def test_push_request_and_approval_against_a_bare_remote(client, repos, tmp_path):
    _, mine = repos
    bare = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(bare))
    git(mine, "remote", "add", "pub", str(bare))
    git(mine, "checkout", "-q", "-b", "feature")
    commit_file(mine, "f.txt", "x\n", "feature work")
    registry.add_repo(str(mine), None, "mine")
    req = client.post("/api/repo/mine/push/request", json={"remote": "pub"}).get_json()["request"]
    assert req["type"] == "push" and req["branch"] == "feature" and req["commits"][0]["subject"] == "feature work"
    assert git(bare, "branch", "--list") == ""  # nothing pushed before approval
    body = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert body["ok"], body
    assert git(bare, "log", "-1", "--format=%s", "feature") == "feature work"


def test_rejected_push_explains_itself(client, repos, tmp_path):
    up, mine = repos
    bare = tmp_path / "r.git"
    git(tmp_path, "clone", "-q", "--bare", str(up), str(bare))
    git(mine, "remote", "add", "pub", str(bare))
    commit_file(up, "u.txt", "u\n", "upstream moved")
    git(up, "push", "-q", str(bare), "main")
    commit_file(mine, "m.txt", "m\n", "mine")
    with pytest.raises(github.GitHubError, match="fetch and merge first"):
        github.push(str(mine), "pub", "main")


def test_open_pr_and_existing_pr(monkeypatch):
    credentials.set("github_token", "github_pat_X1234567890")
    calls = []

    def fake_api(method, path, token=None, **kw):
        calls.append((method, path))
        if method == "POST":
            raise github.GitHubError("GitHub 422: Validation Failed A pull request already exists")
        return [{"html_url": "https://github.com/o/r/pull/5", "number": 5}]
    monkeypatch.setattr(github, "_api", fake_api)
    pr = github.open_pr("o", "r", "feature", "main", "t", "b")
    assert pr == {"url": "https://github.com/o/r/pull/5", "number": 5, "existing": True}


def test_set_secret_encrypts_for_the_repo_key(monkeypatch):
    from nacl import encoding, public
    key = public.PrivateKey.generate()
    sent = {}

    def fake_api(method, path, token=None, **kw):
        if method == "GET":
            return {"key": key.public_key.encode(encoding.Base64Encoder()).decode(), "key_id": "k1"}
        sent.update(path=path, **kw["json"])
        return {}
    monkeypatch.setattr(github, "_api", fake_api)
    github.set_secret("o", "r", "DOCKERHUB_TOKEN", "dckr_pat_secret")
    assert sent["path"] == "/repos/o/r/actions/secrets/DOCKERHUB_TOKEN" and sent["key_id"] == "k1"
    assert public.SealedBox(key).decrypt(base64.b64decode(sent["encrypted_value"])) == b"dckr_pat_secret"
    with pytest.raises(github.GitHubError):
        github.set_secret("o", "r", "GITHUB_TOKEN", "x")


# ------------------------------------------------------------------ workflows
def _spec(**kw):
    s = {"images": [{"image": "app-gateway", "context": "gateway", "dockerfile": "gateway/Dockerfile"},
                    {"image": "app-mcp", "context": "gateway/mcp", "dockerfile": "gateway/mcp/Dockerfile"}],
         "owner": "Wizard815", "ghcr": True, "dockerhub": True, "arm64": True, "filename": "docker-images.yml"}
    s.update(kw)
    return s


def test_render_is_valid_and_complete():
    doc = yaml.safe_load(workflows.render(_spec()))
    on = doc.get("on", doc.get(True))
    assert set(on) == {"push", "pull_request", "workflow_dispatch"} and on["push"]["tags"] == ["v*"]
    job = doc["jobs"]["image"]
    assert [m["image"] for m in job["strategy"]["matrix"]["include"]] == ["app-gateway", "app-mcp"]
    names = [s["name"] for s in job["steps"]]
    assert "Log in to GHCR" in names and "Log in to Docker Hub" in names and "Set up QEMU (arm64 builds)" in names
    build = next(s for s in job["steps"] if s["name"] == "Build and push")["with"]
    assert build["platforms"] == "linux/amd64,linux/arm64" and "pull_request" in job["env"]["PUSH"]
    meta = next(s for s in job["steps"] if s["name"] == "Image names and tags")["with"]
    assert "ghcr.io/wizard815/${{ matrix.image }}" in meta["images"]  # GHCR needs lowercase
    assert doc["permissions"]["packages"] == "write"


def test_render_ghcr_only_has_no_docker_hub_step():
    doc = yaml.safe_load(workflows.render(_spec(dockerhub=False, arm64=False)))
    names = [s["name"] for s in doc["jobs"]["image"]["steps"]]
    assert "Log in to Docker Hub" not in names and "Set up QEMU (arm64 builds)" not in names


def test_validate_catches_bad_specs():
    errs = workflows.validate(_spec(images=[{"image": "Bad Name", "context": "../x", "dockerfile": "Dockerfile"}],
                                    ghcr=False, dockerhub=False, filename="x.txt"))
    assert any("lowercase" in e for e in errs) and any("inside the repo" in e for e in errs)
    assert any("GHCR, Docker Hub" in e for e in errs) and any("file name" in e for e in errs)


def test_detect_skips_submodules_and_dependencies(tmp_path):
    for p in ("gateway/Dockerfile", "gateway/node_modules/x/Dockerfile", "apps/sub/Dockerfile", "Dockerfile.dev"):
        (tmp_path / p).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / p).write_text("FROM scratch\n")
    found = workflows.detect({"path": str(tmp_path), "components": [{"path": "apps/sub"}]})
    assert [f["dockerfile"] for f in found] == ["Dockerfile.dev", "gateway/Dockerfile"]
    assert all(workflows._NAME.match(f["image"]) for f in found)


def test_commit_writes_workflow_and_removes_only_listed_files(client, repos):
    _, mine = repos
    (mine / ".github" / "workflows").mkdir(parents=True)
    commit_file(mine, ".github/workflows/old.yml", "name: old\non: push\njobs: {}\n", "old wf")
    commit_file(mine, ".github/workflows/ci.yml", "name: ci\non: push\njobs: {}\n", "ci wf")
    registry.add_repo(str(mine), None, "mine")
    head = git(mine, "rev-parse", "HEAD")
    spec = _spec(images=[{"image": "mine", "context": ".", "dockerfile": "Dockerfile"}])
    r = client.post("/api/repo/mine/docker/workflow", json={"spec": spec, "commit": True,
                                                            "remove": [".github/workflows/old.yml", "README.md"]}).get_json()
    assert r["ok"], r
    changed = sorted(git(mine, "diff", "--name-only", head, "HEAD").splitlines())
    assert changed == [".github/workflows/docker-images.yml", ".github/workflows/old.yml"]
    assert (mine / ".github/workflows/ci.yml").exists() and not (mine / ".github/workflows/old.yml").exists()
    assert git(mine, "status", "--porcelain") == ""
