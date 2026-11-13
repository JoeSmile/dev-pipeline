"""Abort cleanup activity. Recycles this execution's workspaces and branches."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.abort_record import AbortCleanupInput, AbortRecord
from dev_pipeline.worktree_recycle import recycle_aborted_execution

logger = structlog.get_logger("dev_pipeline.activities.cleanup")


@activity.defn(name="recycle_aborted_execution")
async def recycle_aborted_execution_activity(payload: AbortCleanupInput) -> AbortRecord:
    """Recycle unmerged worktrees and local branches created by this execution.

    Existing remote PRs are recorded for a human and are not closed or merged.
    Temporal History is not deleted.

    Args:
        payload: Execution identity, config-root, and claimed workspace targets.

    Returns:
        A replayable abort record for the existing parent snapshot.
    """

    record = recycle_aborted_execution(payload)
    logger.info(
        "abort 已回收本执行工作区",
        execution_id=payload.execution_id,
        recycled=len(record.recycled),
        remote_prs=len(record.remote_prs_pending_human),
    )
    return record
