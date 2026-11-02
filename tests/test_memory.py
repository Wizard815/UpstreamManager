"""SQLite memory: keyword search, file/commit links, kept chat messages and day logs."""
from datetime import date, timedelta

import pytest

from upstream_upkeep import config, llm, memory, registry, store


@pytest.fixture
def mem(tmp_path, monkeypatch):
    monkeypatch.setenv("UPSTREAM_MEMORY", "on")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state.json")
    memory.reload()
    yield memory
    memory.reload()
    monkeypatch.setenv("UPSTREAM_MEMORY", "off")


def test_keyword_search_ranks_and_filters(mem):
    mem.remember("a", "User prefers small pull requests that touch one module", kind="note")
    mem.remember("a", "Daily check merged 3 commits from upstream", kind="event")
    mem.remember("b", "User wants pull requests squashed", kind="note")
    hits = mem.search("how big should my pull requests be?", repo_id="a")
    assert hits and hits[0]["memory"].startswith("User prefers small pull requests")
    assert all(h["repo"] == "a" for h in hits)
    assert [h["repo"] for h in mem.search("pull requests", exclude="a")] == ["b"]
    assert mem.search("pull requests", repo_id="a", kind="event") == []


def test_exact_repeats_are_stored_once(mem):
    first = mem.remember("a", "Never force-push to main", kind="note")
    again = mem.remember("a", "Never force-push to main", kind="note")
    assert first[0]["id"] == again[0]["id"] and len(mem.list_repo("a")) == 1


def test_files_and_commits_link_across_repos(mem):
    mem.remember("b", "Commit 1a2b3c4d on 2026-10-01 by T: fix login. Files: src/auth/login.py", kind="commit")
    # Named by bare file name or by commit id, in a message with no shared words.
    for q in ("what changed in login.py?", "explain 1a2b3c4d"):
        hit = mem.search(q, exclude="a")
        assert hit and hit[0]["score"] == 1.0 and hit[0]["repo"] == "b"
        assert llm._relevant(hit[0], llm.OTHER_REPO_MIN_SCORE)


def test_refs_finds_paths_and_shas():
    r = memory.refs("Edited src/app.py and README.md in 9f3e2a1 (see https://x.io/a.html), v1.2")
    assert {"src/app.py", "app.py", "readme.md", "9f3e2a1"} <= r
    assert "1.2" not in r and not any(x.startswith("http") for x in r)


def test_forget_and_cleanup_keep_day_logs(mem):
    keep = mem.remember("a", "Log for 2026-10-01:\n\nWorked on x", kind="daylog", day="2026-10-01")
    dup = mem.remember("a", "Commit 1a2b3c4d x", kind="chat")  # a fact restating a commit
    res = mem.cleanup("a")
    assert res["removed"] == 1 and [i["id"] for i in mem.list_repo("a")] == [keep[0]["id"]]
    mem.forget(keep[0]["id"])
    assert mem.list_repo("a") == [] and dup


def test_messages_survive_deleted_sessions_and_get_logged(mem, repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    ts = config.now_iso()
    mem.record_message("mine", "s1", "user", "Why does src/app.py fail to start?", ts)
    mem.record_message("mine", "s1", "assistant", "The PORT variable is not a number.", ts[:-6] + "1" + ts[-6:])
    mem.remember("mine", "Daily check raised an approval request", kind="event")
    seen = {}

    def fake_complete(system, user, max_tokens, temperature=0.1):
        seen["system"], seen["user"] = system, user
        return "**Worked on** - startup failure in src/app.py (PORT not a number)"
    monkeypatch.setattr(memory, "_complete", fake_complete)
    store.delete_session("mine", "s1")  # the chat is gone from the app, not from memory

    res = mem.write_day_log("mine")
    assert res["ok"] and res["sources"] == 3
    assert "PORT variable" in seen["user"] and "approval request" in seen["user"] and "Worked on" in seen["system"]
    logs = mem.list_repo("mine", kind="daylog")
    assert len(logs) == 1 and logs[0]["day"] == date.today().isoformat()
    # Rewriting today's log replaces it, and the log is found by the file it covers.
    assert mem.write_day_log("mine")["ok"] and len(mem.list_repo("mine", kind="daylog")) == 1
    assert mem.search("app.py", repo_id="mine", kind="daylog")


def test_due_logs_cover_finished_days_once(mem, repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    mem.record_message("mine", "s1", "user", "Rebase the feature branch", f"{yesterday}T12:00:00")
    mem.record_message("mine", "s2", "user", "today's question", config.now_iso())
    calls = []
    monkeypatch.setattr(memory, "_complete", lambda *a, **k: calls.append(a[1]) or "**Worked on** - rebase")
    done = mem.write_due_logs()
    assert [(d["day"], d["ok"]) for d in done] == [(yesterday, True)]  # today is not finished yet
    assert "Rebase the feature branch" in calls[0]
    assert mem.write_due_logs() == []  # already logged


def test_due_logs_stop_when_the_model_is_down(mem, repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    for n in (2, 3):
        day = (date.today() - timedelta(days=n)).isoformat()
        mem.record_message("mine", "s", "user", f"work {n}", f"{day}T10:00:00")

    def down(*a, **k):
        raise ConnectionError("refused")
    monkeypatch.setattr(memory, "_complete", down)
    done = mem.write_due_logs()
    assert len(done) == 1 and not done[0]["ok"]
    assert mem.list_repo("mine", kind="daylog") == []


def test_asking_about_a_day_brings_its_log(mem, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    mem.remember("mine", f"Log for {yesterday}:\n\n**Worked on** - the flaky deploy job", kind="daylog", day=yesterday)
    assert memory.days_mentioned("what did we do yesterday?") == [yesterday]
    assert memory.days_mentioned("and on 2026-10-30") == ["2026-10-30"]
    msgs = llm.build_messages(registry.get_repo("mine"), {"messages": []}, "What did we do yesterday?")
    assert "flaky deploy job" in msgs[0]["content"]


def test_existing_chat_history_is_imported(tmp_path, monkeypatch, repos):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    session = store.new_session("mine")
    store.add_chat_message("mine", session["id"], "user", "an old question")
    monkeypatch.setenv("UPSTREAM_MEMORY", "on")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "state.json")
    memory.reload()
    try:
        assert memory.status()["messages"] == 1
    finally:
        memory.reload()
        monkeypatch.setenv("UPSTREAM_MEMORY", "off")


def test_daylog_endpoint(client, mem, repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    assert client.post("/api/repo/mine/memory/daylog", json={"day": "nope"}).status_code == 400
    r = client.post("/api/repo/mine/memory/daylog", json={}).get_json()
    assert not r["ok"] and "nothing happened" in r["error"]
    mem.record_message("mine", "s", "user", "hello there friend", config.now_iso())
    monkeypatch.setattr(memory, "_complete", lambda *a, **k: "**Worked on** - greetings")
    assert client.post("/api/repo/mine/memory/daylog", json={}).get_json()["ok"]
    items = client.get("/api/repo/mine/memory?kind=daylog").get_json()["items"]
    assert len(items) == 1 and items[0]["kind"] == "daylog"
