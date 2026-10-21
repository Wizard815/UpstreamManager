"""Shared type aliases for the upkeep package."""
from __future__ import annotations


class DailyRunResult:
    """Result of a daily check or push run."""

    def __init__(
        self,
        path: str,
        timestamp: str,
        status: str = "ok",
        error: str | None = None,
        upstream_count: int = 0,
        recent_count: int = 0,
        dirty: bool = False,
        has_local_changes: bool = False,
        committed_sha: str | None = None,
        commit_message: str | None = None,
        committed: bool = False,
        ready_to_push: bool = False,
        pushed: bool = False,
        push_output: str | None = None,
        message: str | None = None,
        merge_conflict: bool = False,
    ):
        self.path = path
        self.timestamp = timestamp
        self.status = status
        self.error = error
        self.upstream_count = upstream_count
        self.recent_count = recent_count
        self.dirty = dirty
        self.has_local_changes = has_local_changes
        self.committed_sha = committed_sha
        self.commit_message = commit_message
        self.committed = committed
        self.ready_to_push = ready_to_push
        self.pushed = pushed
        self.push_output = push_output
        self.message = message
        self.merge_conflict = merge_conflict

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "timestamp": self.timestamp,
            "status": self.status,
            "error": self.error,
            "upstream_count": self.upstream_count,
            "recent_count": self.recent_count,
            "dirty": self.dirty,
            "has_local_changes": self.has_local_changes,
            "committed_sha": self.committed_sha,
            "commit_message": self.commit_message,
            "committed": self.committed,
            "ready_to_push": self.ready_to_push,
            "pushed": self.pushed,
            "push_output": self.push_output,
            "message": self.message,
            "merge_conflict": self.merge_conflict,
        }
