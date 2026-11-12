"""Deterministic stub and coding activities used by the local worker.

Publish remains a TestDeploying stub: it does not call LLMs or talk to
external systems. Story Retro lives in ``activities.retro``. ``implement_ticket``
writes only inside an authorized worktree and does not call Cursor CLI.
Dual review lives in ``activities.review`` and verification lives in
``activities.verify``; both use a deterministic backend, not a live CLI.
Tests may register replacements with the same activity names.
"""

from temporalio import activity

from dev_pipeline.deliverables import produce_implement_result
from dev_pipeline.models import (
    ImplementResult,
    PublishRequest,
    PublishResult,
    TicketWorkInput,
)
from dev_pipeline.progress_log import activity_progress
from dev_pipeline.transitions import validate_publish_target
from dev_pipeline.workspace_auth import resolve_workspace_claim


@activity.defn
async def implement_ticket(payload: TicketWorkInput) -> ImplementResult:
    """Produce ticket deliverables in the authorized worktree, without a live CLI.

    The activity reuses the DP-11 workspace claim so a rerun with the same
    key cannot record a second branch or open a PR. It does not call Cursor
    CLI or read CURSOR_API_KEY.
    """

    async with activity_progress(
        config_root=payload.config_root,
        execution_id=payload.execution_id,
        ticket_id=payload.ticket_id,
        activity_name="implement_ticket",
        initial_step="正在准备交付物",
    ) as progress:
        claim = resolve_workspace_claim(payload)
        progress.update(f"正在写代码：实现{payload.ticket_id}")
        result = produce_implement_result(payload, claim.authorized_worktree)
        progress.update("正在收集 diff 与测试证据")
        activity.logger.info(
            "implement_ticket ticket=%s complete=%s",
            payload.ticket_id,
            bool(result.deliverables and result.deliverables.complete),
        )
        return result


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
