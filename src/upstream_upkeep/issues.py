"""GitHub and home-forge issues as Work tasks.

A repo's issues come from the GitHub or forge repo behind one of its remotes (origin by default, else
the first upstream on github.com or the forge; the user can pick another). Forge issues need the forge
token (Settings -> Accounts). Importing one makes a task with the issue's
title and text and a link back to it; an issue already imported is not imported twice. Public repos
work without a token (GitHub allows 60 requests an hour); a saved token raises that and opens private
repos. Nothing is written to GitHub.
"""
from __future__ import annotations

from . import actions, forge, github, store, tasks

MAX_IMPORT = 50
_KIND_BY_LABEL = {"bug": "bug", "defect": "bug", "regression": "bug", "enhancement": "feature", "feature": "feature",
                  "feature request": "feature", "chore": "chore", "maintenance": "chore", "idea": "idea", "question": "idea"}


class IssueError(ValueError):
    pass


def remotes(repo: dict) -> list[dict]:
    """The repo's remotes on github.com or the home forge: [{remote, slug, forge}], origin first."""
    seen, out = set(), []
    cands = [("origin", repo.get("remote_url"))] + [(u["name"], u.get("url")) for u in repo.get("upstreams") or []]
    for name, url in cands:
        hit = forge.parse(url or "")
        if hit and name not in seen:
            seen.add(name)
            out.append({"remote": name, "slug": f"{hit.owner}/{hit.repo}", "forge": hit.forge})
    return out


def _fetch_list(src: dict, state: str, limit: int) -> list:
    if src.get("forge") == "forge":
        owner, name = src["slug"].split("/", 1)
        return forge.issues(owner, name, state, limit)
    return actions._get(f"/repos/{src['slug']}/issues", {"state": state, "per_page": max(1, min(int(limit), 100)),
                                                       "sort": "updated"})


def _fetch_one(src: dict, n: int) -> dict:
    if src.get("forge") == "forge":
        owner, name = src["slug"].split("/", 1)
        return forge.issue(owner, name, n)
    return actions._get(f"/repos/{src['slug']}/issues/{n}")


def _is_pr(i: dict, src: dict) -> bool:
    # GitHub adds pull_request only to PRs; Forgejo sends it as null on plain issues.
    return i.get("pull_request") is not None if src.get("forge") == "forge" else "pull_request" in i


def _source(repo: dict, remote: str | None) -> dict:
    rs = remotes(repo)
    if not rs:
        raise IssueError("this repo has no remote on github.com or the home forge")
    if remote:
        for r in rs:
            if r["remote"] == remote:
                return r
        raise IssueError(f"{remote} is not a github.com or forge remote of this repo")
    return rs[0]


def _imported(repo_id: str) -> dict[str, str]:
    """issue url -> task id, for this repo's tasks made from issues."""
    return {t["source"]["url"]: t["id"] for t in tasks.list_tasks(repo_id) if (t.get("source") or {}).get("type") == "github-issue"}


def _shape(i: dict, done: dict) -> dict:
    return {"number": i["number"], "title": i.get("title") or "", "url": i.get("html_url"), "state": i.get("state"),
            "labels": [lb.get("name") for lb in i.get("labels") or [] if isinstance(lb, dict)],
            "comments": i.get("comments", 0), "user": (i.get("user") or {}).get("login"),
            "created_at": i.get("created_at"), "updated_at": i.get("updated_at"),
            "body": (i.get("body") or "")[:20000], "task": done.get(i.get("html_url"))}


def list_issues(repo: dict, remote: str | None = None, state: str = "open", limit: int = 50) -> dict:
    src = _source(repo, remote)
    if state not in ("open", "closed", "all"):
        raise IssueError("state is open, closed or all")
    data = _fetch_list(src, state, limit)
    done = _imported(repo["id"])
    items = [_shape(i, done) for i in data if isinstance(i, dict) and not _is_pr(i, src)]   # issues, not PRs
    return {"ok": True, "source": src, "remotes": remotes(repo), "items": items}


def import_issues(repo: dict, numbers: list, remote: str | None = None, by: str = "user") -> dict:
    if not isinstance(numbers, list) or not numbers:
        raise IssueError("name the issue numbers to import")
    try:
        wanted = list(dict.fromkeys(int(n) for n in numbers))[:MAX_IMPORT]
    except (TypeError, ValueError):
        raise IssueError("issue numbers are whole numbers")
    src = _source(repo, remote)
    done = _imported(repo["id"])
    made, skipped = [], []
    for n in wanted:
        try:
            i = _fetch_one(src, n)
        except github.GitHubError as e:
            skipped.append({"number": n, "why": str(e)})
            continue
        if _is_pr(i, src):
            skipped.append({"number": n, "why": "that is a pull request, not an issue"})
            continue
        url = i.get("html_url")
        if url in done:
            skipped.append({"number": n, "why": "already a task", "task": done[url]})
            continue
        labels = [(lb.get("name") or "").lower() for lb in i.get("labels") or [] if isinstance(lb, dict)]
        kind = next((_KIND_BY_LABEL[lb] for lb in labels if lb in _KIND_BY_LABEL), "fix")
        where = "forge" if src.get("forge") == "forge" else "GitHub"
        body = (f"From {where} issue {src['slug']}#{n} by {(i.get('user') or {}).get('login', '?')}: {url}\n"
                + (f"Labels: {', '.join(labels)}\n" if labels else "") + "\n" + (i.get("body") or "").strip())
        t = tasks.create(repo["id"], f"#{n} {i.get('title') or ''}".strip()[:200], body[:20000], kind,
                         [{"type": "issue", "id": url, "label": f"{src['slug']}#{n}"}], by=by)
        with store.lock:
            t["source"] = {"type": "github-issue", "repo": src["slug"], "number": n, "url": url, "forge": src.get("forge", "github")}
            store.save_state()
        done[url] = t["id"]
        made.append(t)
    return {"ok": True, "source": src, "created": made, "skipped": skipped}
