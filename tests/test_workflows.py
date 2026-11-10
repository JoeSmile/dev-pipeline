"""WorkflowEnvironment tests for parent legal path, DP-26 orchestration, and gates."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    exception_text,
    stage_of,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_until,
)

from dev_pipeline.activities.stubs import publish_to_test_env
from dev_pipeline.errors import ProductionDeployForbidden
from dev_pipeline.models import (
    PublishRequest,
    ReviewFindings,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    StoryInput,
    TicketSpec,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


async def test_happy_path_reaches_done_via_test_deploying(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="T-1")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
        assert result.test_deploy_status is TestDeployStatus.SUCCEEDED
        assert result.test_deploy_address
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.tickets[0].stage is TicketStage.VERIFIED
    finally:
        await worker.__aexit__(None, None, None)


async def test_sibling_in_reviewing_blocks_parent_test_deploy_and_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.hold("review", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.VERIFIED
            and stage_of(item, "B") is TicketStage.REVIEWING,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        assert snap.stage not in {ParentStage.TEST_DEPLOYING, ParentStage.RETRO, ParentStage.DONE}
        assert snap.waiting_signal != "正在等候你确认测试环境发布"
        controller.release("review", "B")
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_parent_cannot_enter_retro_when_test_deploy_fails(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    controller.publish_succeeds = False
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.BLOCKED
        assert result.test_deploy_status is TestDeployStatus.FAILED
        assert result.blocked_reason is not None
        assert "不得进入 Retro" in result.blocked_reason
    finally:
        await worker.__aexit__(None, None, None)


async def test_independent_tickets_start_in_parallel(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: started(item, "A") and started(item, "B"),
        )
        assert {t.ticket_id for t in snap.tickets if t.started} == {"A", "B"}
        await wait_until(lambda: set(controller.started) >= {"A", "B"})
        controller.release("implement", "A", "B")
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_dependent_ticket_does_not_start_early(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [
            TicketSpec(ticket_id="A"),
            TicketSpec(ticket_id="B", depends_on=["A"]),
        ],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(handle, lambda item: started(item, "A"))
        assert started(snap, "A")
        assert not started(snap, "B")
        assert stage_of(snap, "B") is TicketStage.PENDING
        assert "B" not in controller.started
        controller.release("implement", "A")
        await wait_snapshot(handle, lambda item: stage_of(item, "A") is TicketStage.VERIFIED)
        snap = await wait_snapshot(handle, lambda item: started(item, "B"))
        assert started(snap, "B")
        controller.release("implement", "B")
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_one_child_failure_does_not_cancel_sibling_and_blocks_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.implement_errors.add("A")
    controller.hold("implement", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.FAILED and started(item, "B"),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        child_b = workflow_env.client.get_workflow_handle(
            ticket_workflow_id("B", payload.execution_id)
        )
        description = await child_b.describe()
        assert description.status.name not in {"CANCELED", "CANCELLED", "TERMINATED", "FAILED"}
        controller.release("implement", "B")
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "B") is TicketStage.VERIFIED
            and stage_of(item, "A") is TicketStage.FAILED,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        await handle.signal(DevPipelineWorkflow.drop_ticket, "A")
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
        assert result.test_deploy_status is TestDeployStatus.SUCCEEDED
    finally:
        await worker.__aexit__(None, None, None)


async def test_missing_config_root_fails_workflow_visibly(
    workflow_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    payload = story(tmp_path / "missing-config", [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        text = exception_text(exc.value)
        assert "配置根目录" in text
        assert "不会当作" in text or "不存在" in text or "必填" in text
    finally:
        await worker.__aexit__(None, None, None)


async def test_empty_config_root_fails_workflow_visibly(
    workflow_env: WorkflowEnvironment,
) -> None:
    payload = StoryInput(
        story_id="STORY-empty",
        execution_id="exec-empty",
        config_root="",
        tickets=[TicketSpec(ticket_id="A")],
    )
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        text = exception_text(exc.value)
        assert "配置根目录" in text
        assert "必填" in text or "不能省略" in text
    finally:
        await worker.__aexit__(None, None, None)


async def test_production_publish_activity_is_rejected() -> None:
    env = ActivityEnvironment()
    with pytest.raises(ProductionDeployForbidden, match="禁止生产发布"):
        await env.run(
            publish_to_test_env,
            PublishRequest(story_id="s", execution_id="e", target="production"),  # type: ignore[arg-type]
        )


async def test_blocking_review_waits_for_human_gate(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    controller.review_findings["A"] = ReviewFindings(blocking=["must gate"], paradigm_drift=False)
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="A", decision="approve"),
        )
        await authorize_test_deploy(handle)
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_abort_from_intake_blocks_and_reopen_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.reopen, "exec-new")
        snap = await wait_snapshot(handle, lambda item: item.last_illegal is not None)
        assert snap.last_illegal is not None
        assert "新的 execution_id" in snap.last_illegal
        await handle.signal(DevPipelineWorkflow.abort, "stop")
        result = await handle.result()
        assert result.stage is ParentStage.BLOCKED
        assert result.blocked_reason is not None
    finally:
        await worker.__aexit__(None, None, None)


async def test_spike_reject_blocks(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, ScopeLock(in_scope="in"))
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            SchemeDecision(has_high_risk_assumption=True, tickets=list(payload.tickets)),
        )
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SPIKE)
        await handle.signal(DevPipelineWorkflow.approve_spike, False)
        result = await handle.result()
        assert result.stage is ParentStage.BLOCKED
    finally:
        await worker.__aexit__(None, None, None)
