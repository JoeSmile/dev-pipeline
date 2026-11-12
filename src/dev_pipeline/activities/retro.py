"""Deterministic Story Retro and promote_rule Activities. No Cursor CLI or LLM."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.retro import PromoteApplyInput, PromoteApplyResult, RetroInput, RetroResult
from dev_pipeline.retro_backend import produce_promote_result, produce_retro_result

logger = structlog.get_logger("dev_pipeline.activities.retro")


@activity.defn(name="write_retro_record")
async def write_retro_record(payload: RetroInput) -> RetroResult:
    """Append this Story's evidence into today's daily note and update candidates.

    The Activity does not call Cursor CLI, does not read CURSOR_API_KEY, and
    does not promote a candidate to L0 or a global Skill.
    """

    activity.heartbeat("writing-retro")
    result = produce_retro_result(payload)
    activity.heartbeat("retro-complete")
    record = result.record
    logger.info(
        "write_retro_record story=%s daily=%s candidates=%s",
        payload.story_id,
        record.daily_path if record is not None else "",
        len(record.candidates) if record is not None else 0,
    )
    return result


@activity.defn(name="apply_promote_rule")
async def apply_promote_rule(payload: PromoteApplyInput) -> PromoteApplyResult:
    """Apply a human promote_rule to a candidate, or reject without promoting."""

    activity.heartbeat("promoting")
    result = produce_promote_result(payload)
    activity.heartbeat("promote-complete")
    logger.info(
        "apply_promote_rule candidate=%s promoted=%s",
        payload.request.candidate_id,
        result.promoted,
    )
    return result
