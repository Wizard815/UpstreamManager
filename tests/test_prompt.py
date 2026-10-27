"""Where things land in the prompt: small local models only reliably use what is
in the user's own turn, so preferences and attachments go there."""
from upstream_upkeep import llm, memory, registry


def test_preferences_go_next_to_the_question(repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    hits = {
        "commit": [{"kind": "commit", "memory": "Commit abc on 2026-01-01: fix x", "score": 0.5, "repo": "mine"}],
        "other": [{"kind": "chat", "memory": "User prefers small PRs that touch only one module", "score": 0.9, "repo": "mine"},
                  {"kind": "note", "memory": "Never force-push to main", "score": 0.4, "repo": "mine"},
                  {"kind": "event", "memory": "Daily check merged 3 commits", "score": 0.6, "repo": "mine"}],
    }

    def fake_search(q, repo_id=None, exclude=None, k=8, kind=None, exclude_kind=None):
        if exclude:
            return []
        return hits["commit"] if kind == "commit" else hits["other"]
    monkeypatch.setattr(memory, "search", fake_search)
    msgs = llm.build_messages(registry.get_repo("mine"), {"messages": []}, "How should I split my next PR?")
    system, user = msgs[0]["content"], msgs[-1]["content"]
    assert "small PRs" in user and "Never force-push" in user and user.endswith("How should I split my next PR?")
    assert "small PRs" not in system
    assert "Commit abc" in system and "Daily check merged" in system  # repo history stays in the background
    assert "Commit abc" not in user


def test_plain_message_without_memories_is_unchanged(repos, monkeypatch):
    _, mine = repos
    registry.add_repo(str(mine), None, "mine")
    monkeypatch.setattr(memory, "search", lambda *a, **k: [])
    msgs = llm.build_messages(registry.get_repo("mine"), {"messages": []}, "hi")
    assert msgs[-1]["content"] == "hi"
