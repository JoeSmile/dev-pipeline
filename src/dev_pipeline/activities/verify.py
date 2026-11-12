"""Deterministic verifying Activity. Does not call Cursor CLI or an LLM."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.verify import VerifyResult, VerifyWorkInput
from dev_pipeline.verify_backend import produce_verify_result

logger = structlog.get_logger("dev_pipeline.activities.verify")


@activity.defn(name="verify_ticket")
async def verify_ticket(payload: VerifyWorkInput) -> VerifyResult:
    """Run red-line checks in the authorized worktree and write UTF-8 evidence.

    The Activity does not call Cursor CLI or read CURSOR_API_KEY. A dry-run
    or History-only outcome is never classified as PASS.
    """

    activity.heartbeat("verifying")
    result = produce_verify_result(payload)
    activity.heartbeat("verify-complete")
    record = result.record
    logger.info(
        "verify_ticket ticket=%s passed=%s path=%s",
        payload.ticket_id,
        result.passed,
        record.evidence_path if record is not None else "",
    )
    return result
