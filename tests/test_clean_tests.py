"""Testing somewhere clean: jobs on the forge's runners and Studio's throwaway sandboxes.

The forge part runs against a fake API; a real Forgejo with a runner is used when FORGE_TEST_URL,
FORGE_TEST_TOKEN and FORGE_TEST_RUNNER are set. The sandbox part talks to a fake Studio over HTTP."""
import base64
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from conftest import git

from upstream_upkeep import credentials, forge, registry, studio

TOKEN = "forgetoken0000000000000000000000000000ab"


@pytest.fixture(autouse=True)
def file_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("UPSTREAM_SECRETS_BACKEND", "file")
    for env in credentials.FIELDS.values():
        monkeypatch.delenv(env, raising=False)
    monkeypatch.delenv("UPSTREAM_FORGE_HOSTS", raising=False)
    monkeypatch.delenv("UPSTREAM_FORGE_JOBS_TOKEN", raising=False)
    monkeypatch.setattr(credentials, "_file", lambda: tmp_path / "secrets.json")


@pytest.fixture
def home():
    credentials.set("forge_url", "http://forgejo:3000/")
    credentials.set("forge_token", TOKEN)


class FakeForgeApi:
    def __init__(self):
        self.calls, self.files, self.secrets, self.repos = [], {}, {}, set()
        self.run_status = "waiting"

    def __call__(self, method, path, token=None, ok404=False, **kw):
        body = kw.get("json")
        self.calls.append((method, path, body))
        if path == "/user":
            return {"login": "wizard"}
        if path.startswith("/repos/wizard/hermes-jobs/contents/"):
            if method == "GET":
                return self.files.get(path) if path in self.files else None
            self.files[path] = {"content": body["content"], "sha": "s1"}
            return {}
        if path == "/repos/w/r":
            return {"full_name": "w/r", "default_branch": "trunk"}
        if path == "/repos/wizard/hermes-jobs":
            return {"name": "hermes-jobs"} if "hermes-jobs" in self.repos else None
        if path == "/user/repos":
            self.repos.add(body["name"])
            return {}
        if path.endswith("/actions/secrets/JOBS_TOKEN"):
            if method == "DELETE":
                return {} if self.secrets.pop("JOBS_TOKEN", None) else None
            self.secrets["JOBS_TOKEN"] = body["data"]
            return {}
        if path.endswith("/dispatches"):
            return {"id": 41, "run_number": 3}
        if path.endswith("/actions/runs/41"):
            return {"status": self.run_status, "title": "t", "html_url": "http://forgejo:3000/wizard/hermes-jobs/actions/runs/3"}
        if path.endswith("/actions/runs/41/jobs"):
            return [{"id": 7, "name": "run", "status": self.run_status, "runs_on": ["gpu"]},
                    {"id": 8, "name": "run-in-image", "status": "skipped"}]
        if path == "/admin/actions/runners":
            return [{"name": "forgebot-1", "status": "idle", "labels": ["ubuntu-latest"]},
                    {"name": "forgebot-2", "status": "idle", "labels": [{"name": "gpu"}]}]
        if path.endswith("/actions/runs/41/cancel"):
            self.run_status = "cancelled"
            return {}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture
def fake_api(monkeypatch, home):
    f = FakeForgeApi()
    monkeypatch.setattr(forge, "_api", f)
    monkeypatch.setattr(forge, "_api_text", lambda path: f"2026-11-27T10:00:00.1Z setup noise\n2026-11-27T10:00:01.2Z === hermes job output ===\n"
                                                           f"2026-11-27T10:00:02.3Z GPU 0: Tesla P4 {TOKEN}\n2026-11-27T10:00:03.4Z ⭐ Run Post step\n")
    return f


def test_job_workflow_is_the_same_file_hermes_writes():
    skill = os.path.join(os.path.dirname(__file__), "..", "..", "hermes-agent", "skills", "software-development", "forgejo", "templates", "hermes-job.yml")
    if not os.path.exists(skill):
        pytest.skip("hermes-agent is not checked out next to this repo")
    assert open(skill, encoding="utf-8").read() == open(forge._TEMPLATE, encoding="utf-8").read()


def test_job_setup_makes_the_private_jobs_repo_workflow_and_secret(fake_api, monkeypatch):
    fake_api.secrets["JOBS_TOKEN"] = TOKEN  # what an older setup left there
    out = forge.job_setup()
    assert out["workflow"] == "created" and out["checkout_token"].startswith("the run's own token")
    assert ("POST", "/user/repos", {"name": "hermes-jobs", "private": True, "auto_init": True,
                                    "description": "Jobs run on the forge's runners (Hermes forgejo skill, Upstream Manager)"}) in fake_api.calls
    path = f"/repos/wizard/hermes-jobs/contents/{forge.JOB_WORKFLOW}"
    assert base64.b64decode(fake_api.files[path]["content"]).decode() == open(forge._TEMPLATE, encoding="utf-8").read()
    # the write token never goes to jobs, and an old copy of it is taken back
    assert "JOBS_TOKEN" not in fake_api.secrets and TOKEN not in str([c[2] for c in fake_api.calls])
    credentials.set("forge_jobs_token", "readonly")
    out = forge.job_setup()
    assert out["workflow"] == "unchanged" and out["checkout_token"] == "UPSTREAM_FORGE_JOBS_TOKEN"
    assert fake_api.secrets["JOBS_TOKEN"] == "readonly"


def test_job_checkout_leaves_no_token_for_the_command():
    wf = open(forge._TEMPLATE, encoding="utf-8").read()
    assert wf.count("uses: actions/checkout@v4") == wf.count("persist-credentials: false") == 2
    assert "secrets.FORGE_TOKEN" not in wf and wf.count("${{ secrets.JOBS_TOKEN || github.token }}") == 2
    # the command gets only its own text, never a secret, in its environment
    for step in wf.split("- name: command")[1:]:
        assert "secrets." not in step.split("steps:")[0]


def test_job_with_a_repo_and_no_ref_sends_its_default_branch(fake_api):
    # the run's own token can't look up another repo's default branch, so Upstream does
    forge.job_run("ls", repo="w/r")
    body = next(c[2] for c in fake_api.calls if c[1].endswith("/dispatches"))
    assert body["inputs"]["repo"] == "w/r" and body["inputs"]["ref"] == "trunk"


def test_repo_job_checks_out_its_forge_copy_and_reports_masked_output(fake_api, client, repos):
    _, mine = repos
    git(mine, "remote", "add", "forge", "http://forgejo:3000/wizard/mine.git")
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/forge/job", json={"command": "nvidia-smi", "runner": "gpu"}).get_json()
    assert r["ok"] and r["run"] == 41 and r["status"] == "waiting" and "no idle runner" in r["note"]
    method, path, body = next(c for c in fake_api.calls if c[1].endswith("/dispatches"))
    assert path == "/repos/wizard/hermes-jobs/actions/workflows/hermes-job.yml/dispatches"
    assert body["inputs"] == {"runner": "gpu", "image": "", "repo": "wizard/mine", "ref": "main", "command": "nvidia-smi", "title": "mine: nvidia-smi"}
    fake_api.run_status = "success"
    done = client.get("/api/forge/jobs/41").get_json()
    assert done["done"] and done["jobs"] == [{"name": "run", "status": "success", "runs_on": ["gpu"]}]
    assert done["output"] == "GPU 0: Tesla P4 ****"


def test_jobs_refuse_bad_input_and_say_what_to_do(fake_api, client, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    assert "no remote on the forge" in client.post("/api/repo/mine/forge/job", json={"command": "x"}).get_json()["error"]
    for body, why in (({"command": ""}, "command"), ({"command": "x", "runner": "a b"}, "runner label"),
                      ({"command": "x", "repo": "../x"}, "OWNER/REPO"), ({"command": "x", "image": "-v /:/h"}, "image")):
        assert why in client.post("/api/forge/jobs", json=body).get_json()["error"]
    credentials.set("forge_jobs_repo", "not-a-repo")
    assert "OWNER/REPO" in client.post("/api/forge/jobs", json={"command": "x"}).get_json()["error"]


def test_runners_lists_labels(fake_api, client):
    assert client.get("/api/forge/runners").get_json()["runners"][1] == {"name": "forgebot-2", "status": "idle", "labels": ["gpu"]}


def test_a_label_no_runner_has_is_refused_instead_of_waiting_forever(fake_api, client):
    r = client.post("/api/forge/jobs", json={"command": "x", "runner": "gpu2"}).get_json()
    assert "no runner has the label 'gpu2'" in r["error"] and "gpu, ubuntu-latest" in r["error"]
    assert not any(c[1].endswith("/dispatches") for c in fake_api.calls)


def test_cancel_and_exit_code(fake_api, client, monkeypatch):
    assert client.post("/api/forge/jobs/41/cancel", json={}).get_json()["status"] == "cancelled"
    fake_api.run_status = "failure"
    monkeypatch.setattr(forge, "_api_text", lambda path: "setup\n=== hermes job output ===\nboom\n⚙️ [runner]: exitcode '7': failure\nskipping post step for 'x'\n")
    r = client.get("/api/forge/jobs/41").get_json()
    assert r["output"] == "boom" and r["exit_code"] == 7


# ------------------------------------------------------------------ sandboxes through Studio
class FakeStudio:
    def __init__(self):
        self.calls, self.exit = [], 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                action = self.path.rsplit("/", 1)[-1]
                outer.calls.append((action, body, self.headers.get("Authorization")))
                reply = {"create": {"sandbox": {"id": "a1b2c3d4e5", "image": "fedora:43"}},
                         "copy_repo": {"sandbox": "a1b2c3d4e5", "path": "/work/mine"},
                         "exec": {"exitCode": outer.exit, "timedOut": False, "output": "2 passed\n", "truncated": False},
                         "remove": {"removed": True}, "list": {"sandboxes": [], "distros": {"fedora": {}}}}.get(action)
                data = json.dumps(reply or {"error": "no"}).encode()
                self.send_response(200 if reply else 400); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.srv.server_port}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()


@pytest.fixture
def fake_studio(monkeypatch):
    f = FakeStudio()
    studio._FROM_STUDIO.clear()
    monkeypatch.setenv("HERMES_STUDIO_URL", f.url)
    monkeypatch.setenv("UPSTREAM_STUDIO_TOKEN", "jwt")
    yield f
    f.srv.shutdown(); f.srv.server_close()


def test_try_in_sandbox_copies_the_pushed_repo_runs_and_removes(fake_studio, client, repos):
    up, mine = repos
    registry.add_repo(str(mine), None, "mine")
    r = client.post("/api/repo/mine/sandbox", json={"command": "make test", "distro": "fedora", "network": "none"}).get_json()
    assert r["ok"] and r["exit_code"] == 0 and r["output"] == "2 passed\n" and not r["kept"]
    actions = [(a, b) for a, b, _ in fake_studio.calls]
    assert actions[0] == ("create", {"distro": "fedora", "image": "", "network": "none", "minutes": 30, "purpose": "Upstream Manager: mine"})
    assert actions[1] == ("copy_repo", {"sandbox": "a1b2c3d4e5", "repo": str(up), "ref": "main"})
    assert actions[2][0] == "exec" and actions[2][1]["workdir"] == "/work/mine" and actions[2][1]["command"] == "make test"
    assert actions[3] == ("remove", {"sandbox": "a1b2c3d4e5"})
    assert all(auth == "Bearer jwt" for *_, auth in fake_studio.calls)
    fake_studio.exit = 2
    kept = client.post("/api/repo/mine/sandbox", json={"command": "make test", "keep": True}).get_json()
    assert not kept["ok"] and kept["exit_code"] == 2 and kept["kept"] and fake_studio.calls[-1][0] == "exec"


def test_sandbox_actions_pass_through_and_unknown_ones_stop_here(fake_studio, client):
    assert client.get("/api/sandboxes").get_json()["distros"] == {"fedora": {}}
    assert "unknown sandbox action" in client.post("/api/sandboxes/mount_host", json={}).get_json()["error"]
    assert not any(a == "mount_host" for a, *_ in fake_studio.calls)


def test_sandbox_without_studio_says_so(client, monkeypatch):
    monkeypatch.delenv("HERMES_STUDIO_URL", raising=False)
    studio._FROM_STUDIO.clear()
    assert "isn't set up" in client.get("/api/sandboxes").get_json()["error"]


# ------------------------------------------------------------------ a real forge with a runner
@pytest.fixture
def live_forge():
    u, t, label = os.environ.get("FORGE_TEST_URL"), os.environ.get("FORGE_TEST_TOKEN"), os.environ.get("FORGE_TEST_RUNNER")
    if not (u and t and label):
        pytest.skip("set FORGE_TEST_URL, FORGE_TEST_TOKEN and FORGE_TEST_RUNNER (a runner label) to run against a real forge")
    credentials.set("forge_url", u)
    credentials.set("forge_token", t)
    return label


def test_live_job_runs_on_a_real_runner(live_forge):
    assert forge.job_setup()["ok"]
    run = forge.job_run("echo upstream-$((40+2)) && uname -s", runner=live_forge, title="upstream live test")
    deadline = time.time() + 300
    while not run["done"] and time.time() < deadline:
        time.sleep(3)
        run = forge.job_status(run["run"])
    assert run["status"] == "success", run
    assert run["output"].splitlines()[:2] == ["upstream-42", "Linux"]
