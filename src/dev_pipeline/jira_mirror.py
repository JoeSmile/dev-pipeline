"""Deterministic Jira stage-mirror formatting and idempotent comment updates.

This module has no network or filesystem I/O. HTTP access lives in
``dev_pipeline.jira_client`` and is called only from Activities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

STAGE_MIRROR_PREFIX = "【DevPipeline 阶段镜像】"
LAG_MARKER_PREFIX = "【DevPipeline 镜像滞后】"
LAG_MARKER_LABEL = "镜像滞后"

_STAGE_FOOTER = "本评论由流水线自动同步，Jira 只是镜像，不是编排器。阶段以 Temporal History 为准。"
_LAG_FOOTER = "同步未完成。阶段以 Temporal History 为准，请勿把本票当作编排器。"


@dataclass
class JiraStageMirrorInput:
    """Activity input for mirroring one parent stage. Contains no secrets."""

    issue_key: str
    stage: str
    execution_id: str
    config_root: str


@dataclass
class JiraStageMirrorResult:
    """Activity result after a successful mirror write. Contains no secrets."""

    issue_key: str
    stage: str
    comment_updated: bool
    field_updated: bool = False


@dataclass
class JiraLagMarkerInput:
    """Activity input for a best-effort lag marker on the Jira issue."""

    issue_key: str
    stage: str
    execution_id: str
    config_root: str
    reason: str


@dataclass(frozen=True)
class JiraComment:
    """One Jira comment as plain text. Used by the mirror and fakes."""

    comment_id: str
    body: str


class JiraIssueStore(Protocol):
    """Minimal issue operations needed to mirror a stage."""

    def list_comments(self, issue_key: str) -> list[JiraComment]:
        """Return comments on the issue, oldest first."""

    def create_comment(self, issue_key: str, body: str) -> JiraComment:
        """Create a comment and return the stored record."""

    def update_comment(self, issue_key: str, comment_id: str, body: str) -> JiraComment:
        """Replace the body of an existing comment."""

    def set_field(self, issue_key: str, field: str, value: str) -> None:
        """Write a custom field. The field id/name is configured, and 待证."""


def stage_comment_body(stage: str, execution_id: str) -> str:
    """Return the idempotent, prefixed comment body for a stage."""

    return (
        f"{STAGE_MIRROR_PREFIX}\n"
        f"当前阶段: {stage}\n"
        f"execution_id: {execution_id}\n"
        f"{_STAGE_FOOTER}"
    )


def lag_comment_body(stage: str, execution_id: str, reason: str) -> str:
    """Return the prefixed lag-marker comment body."""

    return (
        f"{LAG_MARKER_PREFIX}\n"
        f"{LAG_MARKER_LABEL}。未能把阶段 {stage} 同步到本票。\n"
        f"execution_id: {execution_id}\n"
        f"{reason}\n"
        f"{_LAG_FOOTER}"
    )


def mirror_stage(
    store: JiraIssueStore,
    payload: JiraStageMirrorInput,
    *,
    stage_field: str | None = None,
) -> JiraStageMirrorResult:
    """Write or update the stage-mirror comment, and optionally a custom field."""

    body = stage_comment_body(payload.stage, payload.execution_id)
    existing = _find_comment(
        store.list_comments(payload.issue_key),
        STAGE_MIRROR_PREFIX,
        payload.execution_id,
    )
    updated = existing is not None
    if existing is None:
        store.create_comment(payload.issue_key, body)
    else:
        store.update_comment(payload.issue_key, existing.comment_id, body)
    field_updated = False
    if stage_field:
        store.set_field(payload.issue_key, stage_field, payload.stage)
        field_updated = True
    return JiraStageMirrorResult(
        issue_key=payload.issue_key,
        stage=payload.stage,
        comment_updated=updated,
        field_updated=field_updated,
    )


def mark_mirror_lag(store: JiraIssueStore, payload: JiraLagMarkerInput) -> None:
    """Post an idempotent lag marker so the Jira issue shows sync did not finish."""

    body = lag_comment_body(payload.stage, payload.execution_id, payload.reason)
    existing = _find_lag_comment(
        store.list_comments(payload.issue_key),
        payload.execution_id,
        payload.stage,
    )
    if existing is None:
        store.create_comment(payload.issue_key, body)
        return
    store.update_comment(payload.issue_key, existing.comment_id, body)


def _find_comment(
    comments: list[JiraComment],
    prefix: str,
    execution_id: str,
) -> JiraComment | None:
    marker = f"execution_id: {execution_id}"
    matches = [item for item in comments if item.body.startswith(prefix) and marker in item.body]
    return matches[-1] if matches else None


def _find_lag_comment(
    comments: list[JiraComment],
    execution_id: str,
    stage: str,
) -> JiraComment | None:
    marker = f"execution_id: {execution_id}"
    matches = [
        item
        for item in comments
        if item.body.startswith(LAG_MARKER_PREFIX)
        and marker in item.body
        and stage in item.body
    ]
    return matches[-1] if matches else None


@dataclass
class InMemoryJira:
    """Fake issue store used by unit tests and WorkflowEnvironment stubs."""

    _comments: dict[str, list[JiraComment]] = field(default_factory=dict)
    _fields: dict[str, dict[str, str]] = field(default_factory=dict)
    _next_id: int = 1

    def list_comments(self, issue_key: str) -> list[JiraComment]:
        """Return a copy of comments for the issue."""

        return list(self._comments.get(issue_key, []))

    def create_comment(self, issue_key: str, body: str) -> JiraComment:
        """Store a new comment and assign a monotonic id."""

        comment = JiraComment(comment_id=str(self._next_id), body=body)
        self._next_id += 1
        self._comments.setdefault(issue_key, []).append(comment)
        return comment

    def update_comment(self, issue_key: str, comment_id: str, body: str) -> JiraComment:
        """Replace an existing comment body, or raise if it is missing."""

        items = self._comments.setdefault(issue_key, [])
        for index, item in enumerate(items):
            if item.comment_id == comment_id:
                updated = JiraComment(comment_id=comment_id, body=body)
                items[index] = updated
                return updated
        raise KeyError(comment_id)

    def set_field(self, issue_key: str, field_name: str, value: str) -> None:
        """Write one custom field on the fake issue."""

        self._fields.setdefault(issue_key, {})[field_name] = value

    def comments(self, issue_key: str) -> list[JiraComment]:
        """Public alias used by tests to inspect comments."""

        return self.list_comments(issue_key)

    def fields(self, issue_key: str) -> dict[str, str]:
        """Return the custom fields written on the fake issue."""

        return dict(self._fields.get(issue_key, {}))

    def current_stage_text(self, issue_key: str) -> str | None:
        """Return the stage shown by the latest stage-mirror comment, if any."""

        matches = [
            item
            for item in self.list_comments(issue_key)
            if item.body.startswith(STAGE_MIRROR_PREFIX)
        ]
        if not matches:
            return None
        for line in matches[-1].body.splitlines():
            if line.startswith("当前阶段: "):
                return line.removeprefix("当前阶段: ")
        return None

    def dump(self) -> str:
        """Return a debug rendering that must never include secrets."""

        return repr({"comments": self._comments, "fields": self._fields})
