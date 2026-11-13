"""Abort snapshot types. Pure data; workflow-safe and free of filesystem I/O."""

from __future__ import annotations

from dataclasses import dataclass, field

from dev_pipeline.models import PENDING_FIELD

PR_MARKING_PENDING = PENDING_FIELD
PR_HUMAN_PENDING = "待人工处理"
ABORT_HISTORY_PRESERVED = "History 仍可回放，未删除"


@dataclass
class AbortWorkspaceTarget:
    """One workspace or branch created by the aborted execution."""

    ticket_id: str
    execution_id: str
    worktree: str = ""
    branch_name: str = ""
    remote_pr_url: str = ""
    remote_pr_opened: bool = False


@dataclass
class RecycledWorkspace:
    """Outcome of recycling one execution-created workspace or branch."""

    ticket_id: str
    execution_id: str
    worktree: str = ""
    branch_name: str = ""
    worktree_removed: bool = False
    branch_deleted: bool = False
    skipped_reason: str = ""


@dataclass
class RemotePrHumanPending:
    """An existing remote PR that abort must not close or merge.

    The concrete marking method (label, comment, reviewer request, and so on)
    stays ``待证``. The snapshot only records that a human must handle it.
    """

    ticket_id: str
    branch_name: str
    pr_ref: str
    status: str = PR_HUMAN_PENDING
    marking_method: str = PR_MARKING_PENDING
    closed: bool = False
    merged: bool = False


@dataclass
class AbortCleanupInput:
    """Activity input for abort cleanup. Contains no secrets."""

    story_id: str
    execution_id: str
    config_root: str
    reason: str
    aborted_at: str
    workspaces: list[AbortWorkspaceTarget] = field(default_factory=list)


@dataclass
class AbortRecord:
    """Queryable, replayable abort record on the existing parent snapshot."""

    reason: str
    aborted_at: str
    execution_id: str
    recycled: list[RecycledWorkspace] = field(default_factory=list)
    remote_prs_pending_human: list[RemotePrHumanPending] = field(default_factory=list)
    history_preserved: bool = True
    detail: str = ""
