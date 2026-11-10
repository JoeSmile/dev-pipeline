"""Deterministic stub activities for the parent/child skeleton.

These do not call LLMs, do not write files, and do not talk to external systems.
Tests may register replacements with the same activity names.
"""

from temporalio import activity

from dev_pipeline.models import (
    ImplementResult,
    PublishRequest,
    PublishResult,
    RetroInput,
    RetroResult,
    ReviewFindings,
    SpikeInput,
    SpikeResult,
    TicketWorkInput,
    VerifyResult,
)
from dev_pipeline.transitions import validate_publish_target


@activity.defn
async def run_spike(payload: SpikeInput) -> SpikeResult:
    """Default spike stub: no red-line failure. Tests may replace this."""

    activity.logger.info("stub run_spike story=%s", payload.story_id)
    return SpikeResult(red_line_failed=False, detail="stub-spike")


@activity.defn
async def implement_ticket(payload: TicketWorkInput) -> ImplementResult:
    """Default coding stub. Real Coding workers are later tickets."""

    activity.logger.info("stub implement_ticket ticket=%s", payload.ticket_id)
    return ImplementResult(diff_submitted=True, detail="stub-implement")


@activity.defn
async def review_ticket(payload: TicketWorkInput) -> ReviewFindings:
    """Default review stub: advisory only, so the child may go straight to Verifying."""

    activity.logger.info("stub review_ticket ticket=%s", payload.ticket_id)
    return ReviewFindings(info=["stub-review: no blocking findings"])


@activity.defn
async def verify_ticket(payload: TicketWorkInput) -> VerifyResult:
    """Default verify stub: pass with a placeholder evidence line."""

    activity.logger.info("stub verify_ticket ticket=%s", payload.ticket_id)
    return VerifyResult(passed=True, evidence="stub-verify: red-line placeholder")


@activity.defn
async def publish_to_test_env(request: PublishRequest) -> PublishResult:
    """Publish only to the test environment. Production targets are rejected."""

    validate_publish_target(request.target)
    activity.logger.info(
        "stub publish_to_test_env story=%s target=%s", request.story_id, request.target
    )
    return PublishResult(
        status="succeeded",
        address=f"http://test.local/stories/{request.story_id}",
        detail="stub-test-deploy",
    )


@activity.defn
async def write_retro_record(payload: RetroInput) -> RetroResult:
    """Record that retro would run. The stub itself writes no files."""

    activity.logger.info("stub write_retro_record story=%s", payload.story_id)
    return RetroResult(recorded=True, detail="stub-retro")
