"""Pull reports (no-issue vs conflicting commits, clean prefix) and model-proposed edits."""
from pathlib import Path

from conftest import commit_file, git

from upstream_upkeep import edits, gitops, llm, pull_report, registry, store


# ------------------------------------------------------------------ pull report
def _diverged(repos):
    """Upstream: b (clean), a (conflicts with mine), c (clean). Mine: one edit to a."""
    up, mine = repos
    commit_file(up, "b.txt", "b\n", "up adds b")
    commit_file(up, "a.txt", "one\nUP\nthree\n", "up edits a")
    commit_file(up, "c.txt", "c\n", "up adds c")
    commit_file(mine, "a.txt", "one\nMINE\nthree\n", "my edit to a")
    git(mine, "fetch", "-q", "origin")
    return up, mine


def test_report_splits_no_issue_from_conflicting(repos):
    _, mine = _diverged(repos)
    r = pull_report.analyze(str(mine), "main", "refs/remotes/origin/main")
    assert (r["incoming"], r["no_issue"], r["conflicting"], r["yours"]) == (3, 2, 1, 1)
    assert r["conflict_files"] == ["a.txt"]
    assert [c["subject"] for c in r["conflicting_sample"]] == ["up edits a"]
    cp = r["clean_prefix"]
    assert cp["commits"] == 1 and not cp["complete"]
    assert git(mine, "log", "-1", "--format=%s", cp["sha"]) == "up adds b"
    assert git(mine, "status", "--porcelain") == ""  # read-only


def test_report_fast_forward_has_no_conflicts(repos):
    up, mine = repos
    commit_file(up, "a.txt", "changed\n", "up edits a")
    git(mine, "fetch", "-q", "origin")
    r = pull_report.analyze(str(mine), "main", "refs/remotes/origin/main")
    assert r["fast_forward"] and r["conflicting"] == 0 and r["clean_prefix"]["complete"]


def test_pulling_the_clean_part_merges_only_up_to_it(client, repos):
    _, mine = _diverged(repos)
    registry.add_repo(str(mine), None, "mine")
    rep = client.get("/api/repo/mine/pull-report?upstream=origin").get_json()
    target = rep["clean_prefix"]["sha"]
    req = client.post("/api/repo/mine/upstream/origin/request-pull", json={"target": target}).get_json()["request"]
    body = client.post(f"/api/request/{req['id']}/decide", json={"decision": "approve"}).get_json()
    assert body["ok"], body
    assert gitops.behind_ahead(str(mine), "main", "refs/remotes/origin/main")[0] == 2  # a and c still to come
    assert Path(mine, "b.txt").exists() and not Path(mine, "c.txt").exists()
    assert git(mine, "status", "--porcelain") == ""


# ------------------------------------------------------------------ edits
REPLY = """Here is the change.

EDIT `docs/notes.md`
```
<<<<<<< SEARCH
line two
=======
line 2 (edited)
>>>>>>> REPLACE
```

EDIT new.txt
<<<<<<< SEARCH
=======
brand new
>>>>>>> REPLACE
"""


def test_parse_keeps_dot_directories():
    text = "EDIT ./.github/workflows/ci.yml\n<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE"
    assert edits.parse(text)[0]["path"] == ".github/workflows/ci.yml"


def test_parse_handles_fences_and_backticks():
    found = edits.parse(REPLY)
    assert [e["path"] for e in found] == ["docs/notes.md", "new.txt"]
    assert found[0]["search"] == "line two" and found[0]["replace"] == "line 2 (edited)"
    assert found[1]["search"] == ""


def test_apply_and_undo_keep_crlf_and_bytes(tmp_path):
    (tmp_path / "docs").mkdir()
    original = "line one\r\nline two  \r\nline three\r\n"
    (tmp_path / "docs" / "notes.md").write_bytes(original.encode())
    res = edits.apply(str(tmp_path), edits.parse(REPLY))
    assert res["ok"], res
    # exact substring match: only "line two" is replaced; the line's trailing spaces and CRLF stay
    assert (tmp_path / "docs" / "notes.md").read_bytes() == b"line one\r\nline 2 (edited)  \r\nline three\r\n"
    assert (tmp_path / "new.txt").read_text() == "brand new\n"
    edits.undo(str(tmp_path), res["change"])
    assert (tmp_path / "docs" / "notes.md").read_bytes() == original.encode()
    assert not (tmp_path / "new.txt").exists()


def test_fuzzy_match_across_line_endings(tmp_path):
    (tmp_path / "f.txt").write_bytes(b"alpha\r\nbeta   \r\ngamma\r\n")
    res = edits.apply(str(tmp_path), [{"path": "f.txt", "search": "alpha\nbeta\ngamma", "replace": "A\nB"}])
    assert res["ok"] and (tmp_path / "f.txt").read_bytes() == b"A\r\nB\r\n"


def test_edits_refuse_bad_targets(tmp_path):
    (tmp_path / "a.txt").write_text("x\nx\n")
    cases = [
        ({"path": "../evil.txt", "search": "", "replace": "x"}, "outside"),
        ({"path": ".git/config", "search": "", "replace": "x"}, "outside"),
        ({"path": "a.txt", "search": "x", "replace": "y"}, "exactly once"),
        ({"path": "a.txt", "search": "", "replace": "y"}, "already exists"),
        ({"path": "missing.txt", "search": "q", "replace": "y"}, "not found"),
    ]
    for e, why in cases:
        p = edits.preview(str(tmp_path), e)
        assert not p["ok"] and why in p["error"], (e, p["error"])


def test_chat_proposes_and_api_applies_and_undoes(client, repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    store.save_settings({"llm": {"base_url": "http://model/v1", "model": "m", "api_key": ""}})

    class Fake:
        def raise_for_status(self):
            pass

        def json(self):
            return {"choices": [{"finish_reason": "stop", "message": {"content":
                "EDIT a.txt\n<<<<<<< SEARCH\ntwo\n=======\nTWO\n>>>>>>> REPLACE"}}]}

    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: Fake())
    r = client.post("/api/repo/mine/chat", json={"message": "uppercase two in a.txt"}).get_json()
    assert r["ok"] and r["edits"][0]["ok"] and "+TWO" in r["edits"][0]["diff"]
    assert Path(mine, "a.txt").read_text() == "one\ntwo\nthree\n"  # nothing written yet
    body = {"session": r["session"], "index": r["index"]}
    assert client.post("/api/repo/mine/edits/apply", json=body).get_json()["ok"]
    assert Path(mine, "a.txt").read_text() == "one\nTWO\nthree\n"
    assert client.post("/api/repo/mine/edits/apply", json=body).status_code == 409
    assert client.post("/api/repo/mine/edits/undo", json=body).get_json()["ok"]
    assert Path(mine, "a.txt").read_text() == "one\ntwo\nthree\n"
