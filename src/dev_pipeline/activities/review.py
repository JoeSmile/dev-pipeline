"""Deterministic dual-review Activity. Does not call Cursor CLI or an LLM."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.models import TicketWorkInput
from dev_pipeline.progress_log import ProgressSession, activity_progress
from dev_pipeline.review import ReviewWorkerReport, ReviewWorkInput
from dev_pipeline.review_backend import produce_review_report
from dev_pipeline.review_cli import workspace_fingerprint
from dev_pipeline.sandbox import apply_review_write
from dev_pipeline.workspace_auth import resolve_workspace_claim

logger = structlog.get_logger("dev_pipeline.activities.review")


def _collect_diff(worktree: str) -> str:
    from dev_pipeline.deliverables import collect_reviewable_diff

    return collect_reviewable_diff(worktree)


@activity.defn(name="review_ticket")
async def review_ticket(payload: ReviewWorkInput) -> ReviewWorkerReport:
    """Run one read-only Review Worker against the authorized worktree diff.

    A requested write is always rejected, even inside the authorized tree.
    The Activity does not call Cursor CLI or read CURSOR_API_KEY.
    """
    async with activity_progress(
        config_root=payload.config_root,
        execution_id=payload.execution_id,
        ticket_id=payload.ticket_id,
        activity_name="review_ticket",
        initial_step="正在读取 diff",
    ) as progress:
        return await _run_review(payload, progress)


async def _run_review(payload: ReviewWorkInput, progress: ProgressSession) -> ReviewWorkerReport:
    claim = resolve_workspace_claim(
        TicketWorkInput(
            ticket_id=payload.ticket_id,
            execution_id=payload.execution_id,
            story_id=payload.story_id,
            config_root=payload.config_root,
        )
    )
    worktree = claim.authorized_worktree
    before = workspace_fingerprint(worktree) if worktree else ""
    write_rejected = False
    write_reason = ""
    if payload.write_path.strip():
        result = apply_review_write(worktree, payload.write_path, payload.write_content)
        write_rejected = not result.allowed
        write_reason = result.record.reason
        logger.warning(
            "review write rejected",
            ticket_id=payload.ticket_id,
            worker_id=payload.worker_id,
            reason=write_reason,
        )
    progress.update("正在审查：对照 L0 与本 Story 约束")
    diff = _collect_diff(worktree)
    unchanged = (not worktree) or workspace_fingerprint(worktree) == before
    report = produce_review_report(
        payload.worker_id,
        diff,
        worktree=worktree,
        write_rejected=write_rejected,
        write_reason=write_reason,
        workspace_unchanged=unchanged,
    )
    activity.logger.info(
        "review_ticket ticket=%s worker=%s blocking=%s",
        payload.ticket_id,
        payload.worker_id,
        report.has_block(),
    )
    return report
