"""Test setup: an isolated state file, memory off, and throwaway git repos.

The env vars must be set before the app is imported (config reads them at import).
"""
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

_STATE_DIR = tempfile.mkdtemp(prefix="upkeep-test-")
os.environ["UPSTREAM_STATE_FILE"] = os.path.join(_STATE_DIR, "state.json")
os.environ["UPSTREAM_MEMORY"] = "off"

from upstream_upkeep import store  # noqa: E402
from upstream_upkeep.app import flask_app  # noqa: E402


def git(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def commit_file(repo, name, text, msg):
    Path(repo, name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg)


@pytest.fixture(autouse=True)
def fresh_state():
    store._STATE = {"repos": [], "settings": {}}
    yield


@pytest.fixture
def client():
    return flask_app.test_client()


@pytest.fixture
def repos(tmp_path):
    """An 'up' repo and a clone 'mine' with the clone's origin fetched. Returns (up, mine)."""
    up, mine = tmp_path / "up", tmp_path / "mine"
    up.mkdir()
    git(up, "init", "-q", "-b", "main")
    for r in (up,):
        git(r, "config", "user.email", "t@t.t")
        git(r, "config", "user.name", "T")
    commit_file(up, "a.txt", "one\ntwo\nthree\n", "base")
    git(tmp_path, "clone", "-q", str(up), str(mine))
    git(mine, "config", "user.email", "t@t.t")
    git(mine, "config", "user.name", "T")
    return up, mine
