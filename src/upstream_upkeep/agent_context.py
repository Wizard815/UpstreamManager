"""What a coding agent needs to know about a repo, in one place.

`briefing` is the repo's situation as Markdown: branches, upstreams and what
they would bring, your notes and preferences, recent day logs, open requests,
merge workspaces and the rules this app keeps. `conflicts` shows, for each
file a merge would conflict in, what each side changed since they split.
"""
from __future__ import annotations

from . import gitops, llm, memory, store, upstream_check, workspaces

RULES = """Rules this app keeps (follow them):
- Never push, force-push or rewrite history. Pushes and merges into the focus branch only happen
  when the user approves a request in Upstream Manager.
- Do not edit the repo's own working tree to merge upstream. Open a merge workspace, resolve and
  commit there, then submit it: that raises an approval request.
- Keep the user's own changes (the commits ahead of upstream) unless the user says otherwise.
- When a conflict is unclear, explain both sides and ask rather than guess."""

DIFF_CHARS = 6000


def _memories(repo_id: str) -> list[str]:
    lines = []
    prefs = [m for m in memory.list_repo(repo_id, k=300) if m["kind"] in ("note", "chat", "decision")][:25]
    if prefs:
        lines.append("## Your notes and preferences")
        lines += [f"- {m['memory']}" for m in prefs]
    logs = memory.list_repo(repo_id, k=3, kind="daylog")
    if logs:
        lines.append("## Recent day logs")
        for m in logs:
            text = m["memory"]
            lines.append(text[:2500] + (" ...(cut)" if len(text) > 2500 else ""))
    return lines


def briefing(repo: dict) -> str:
    lines = ["# Upstream Manager briefing", "", "## Repository", llm.repo_facts(repo), ""]
    reqs = [r for r in store.get_requests() if r.get("repo_id") == repo["id"] and r.get("status") == "pending"]
    if reqs:
        lines.append("## Waiting for the user's approval")
        for r in reqs:
            what = r.get("type") or "merge"
            ups = "; ".join(f"{u['name']}/{u.get('branch')}: {u.get('behind')} commit(s), {u.get('reason', '')}"
                            for u in r.get("upstreams") or [])
            lines.append(f"- {r['id']} ({what}) {ups}")
        lines.append("")
    ws = workspaces.list_workspaces(repo)
    if ws:
        lines.append("## Merge workspaces")
        lines += [f"- {w['name']} at {w['path']}: {w.get('next_step') or w.get('error')}" for w in ws]
        lines.append("")
    lines += _memories(repo["id"])
    lines += ["", RULES]
    return "\n".join(lines).strip() + "\n"


def _diff(path: str, base: str, side: str, name: str) -> tuple[str, bool]:
    out = gitops._run(["git", "diff", "--end-of-options", base, side, "--", name], cwd=path).stdout
    return out[:DIFF_CHARS], len(out) > DIFF_CHARS


def _touching(path: str, base: str, side: str, name: str) -> list[str]:
    out = gitops._run(["git", "log", "--format=%h %s", "-20", "--end-of-options", f"{base}..{side}", "--", name],
                      cwd=path).stdout
    return [l for l in out.splitlines() if l.strip()]


def conflicts(repo: dict, upstream: str) -> dict:
    """Per conflicting file: both sides' changes since the merge base, and the commits behind them."""
    focus = repo.get("focus_branch") or "main"
    up = next((u for u in repo.get("upstreams", []) if u["name"] == upstream), None)
    if not up:
        raise ValueError(f"no upstream named {upstream}")
    c = upstream_check.compare_upstream(repo["path"], up, focus)
    if c.missing_ref:
        return {"ok": False, "error": c.conflict_detail}
    ref = f"refs/remotes/{c.name}/{c.branch}"
    base = gitops._run(["git", "merge-base", "--end-of-options", focus, ref], cwd=repo["path"]).stdout.strip()
    files = []
    for name in c.conflict_files[:20]:
        ours, ours_cut = _diff(repo["path"], base, focus, name)
        theirs, theirs_cut = _diff(repo["path"], base, ref, name)
        files.append({"file": name,
                      "yours": {"commits": _touching(repo["path"], base, focus, name), "diff": ours, "truncated": ours_cut},
                      "upstream": {"commits": _touching(repo["path"], base, ref, name), "diff": theirs, "truncated": theirs_cut}})
    return {"ok": True, "focus_branch": focus, "upstream": f"{c.name}/{c.branch}", "merge_base": base,
            "behind": c.behind, "ahead": c.ahead, "conflict": c.conflict,
            "conflict_files": c.conflict_files, "files": files}
