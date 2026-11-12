"""Coding-sandbox write activity. Rejected writes are recorded, never swallowed."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.models import SandboxWriteInput, SandboxWriteResult, TicketWorkInput
from dev_pipeline.sandbox import apply_guarded_write, build_sandbox_record
from dev_pipeline.workspace_auth import resolve_workspace_claim

logger = structlog.get_logger("dev_pipeline.activities.sandbox")

_UNAUTH = "未授权工作区，拒绝写入"


def _authorized_worktree(payload: SandboxWriteInput) -> str:
    if payload.authorized_worktree.strip():
        return payload.authorized_worktree.strip()
    claim = resolve_workspace_claim(
        TicketWorkInput(
            ticket_id=payload.ticket_id,
            execution_id=payload.execution_id,
            story_id=payload.story_id,
            config_root=payload.config_root,
        )
    )
    return claim.authorized_worktree


@activity.defn
async def apply_sandbox_write(payload: SandboxWriteInput) -> SandboxWriteResult:
    """Write inside the authorized worktree, or return a recorded rejection.

    The activity does not call Cursor CLI or any LLM. A missing authorization
    or an out-of-bounds path is returned as ``allowed=False`` with a snapshot
    record; the file is not created.

    Args:
        payload: Ticket identity, optional claimed worktree, and the requested write.

    Returns:
        Whether the write was allowed, plus the queryable sandbox record.
    """

    worktree = _authorized_worktree(payload)
    if not worktree:
        result = SandboxWriteResult(
            allowed=False,
            record=build_sandbox_record(
                authorized_worktree="",
                requested_path=payload.path,
                reason=_UNAUTH,
            ),
            written_path="",
        )
        logger.warning(
            "sandbox write rejected",
            ticket_id=payload.ticket_id,
            reason=result.record.reason,
        )
        return result
    result = apply_guarded_write(worktree, payload.path, payload.content)
    if result.allowed:
        logger.info(
            "sandbox write allowed",
            ticket_id=payload.ticket_id,
            status=result.record.status,
        )
    else:
        logger.warning(
            "sandbox write rejected",
            ticket_id=payload.ticket_id,
            reason=result.record.reason,
        )
    return result
