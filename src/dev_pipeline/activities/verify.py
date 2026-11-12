"""Deterministic verifying Activity. Does not call Cursor CLI or an LLM."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.progress_log import activity_progress
from dev_pipeline.verify import VerifyResult, VerifyWorkInput
from dev_pipeline.verify_backend import produce_verify_result

logger = structlog.get_logger("dev_pipeline.activities.verify")


@activity.defn(name="verify_ticket")
async def verify_ticket(payload: VerifyWorkInput) -> VerifyResult:
    """Run red-line checks in the authorized worktree and write UTF-8 evidence.

    The Activity does not call Cursor CLI or read CURSOR_API_KEY. A dry-run
    or History-only outcome is never classified as PASS.
    """

    async with activity_progress(
        config_root=payload.config_root,
        execution_id=payload.execution_id,
        ticket_id=payload.ticket_id,
        activity_name="verify_ticket",
        initial_step="正在跑红线检查",
    ) as progress:
        result = produce_verify_result(payload)
        progress.update("正在写验证证据")
        record = result.record
        logger.info(
            "verify_ticket ticket=%s passed=%s path=%s",
            payload.ticket_id,
            result.passed,
            record.evidence_path if record is not None else "",
        )
        return result
