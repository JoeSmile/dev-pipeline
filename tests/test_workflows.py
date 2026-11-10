"""WorkflowEnvironment tests for parent legal path, DP-26 orchestration, and gates."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Sequence
from pathlib import Path

import pytest
from temporalio.client import WorkflowFailureError, WorkflowHandle
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker
from tests.stub_activities import StubController

from dev_pipeline.activities.stubs import publish_to_test_env
from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.errors import ProductionDeployForbidden
from dev_pipeline.models import (
    PipelineResult,
    PipelineSnapshot,
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
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = pytest.mark.asyncio


async def _wait_snapshot(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    predicate: object,
    *,
    timeout: float = 15.0,
) -> PipelineSnapshot:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last: PipelineSnapshot | None = None
    while loop.time() < deadline:
        last = await handle.query(DevPipelineWorkflow.snapshot)
        if predicate(last):  # type: ignore[operator]
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for snapshot, last={last}")


def _story(
    config_root: Path,
    tickets: Sequence[TicketSpec],
    *,
    suffix: str | None = None,
) -> StoryInput:
    token = suffix or uuid.uuid4().hex[:8]
    return StoryInput(
        story_id=f"STORY-{token}",
        execution_id=f"exec-{token}",
        config_root=str(config_root),
        tickets=list(tickets),
        summary="test story",
    )


async def _start(
    env: WorkflowEnvironment,
    payload: StoryInput,
    controller: StubController,
    *,
    queue: str | None = None,
) -> tuple[Worker, WorkflowHandle[DevPipelineWorkflow, PipelineResult]]:
    task_queue = queue or f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=f"story-{payload.story_id}-{payload.execution_id}",
        task_queue=task_queue,
    )
    return worker, handle


async def _advance_to_implementing(
    handle: WorkflowHandle[DevPipelineWorkflow, PipelineResult],
    payload: StoryInput,
    *,
    high_risk: bool = False,
) -> PipelineSnapshot:
    await handle.signal(
        DevPipelineWorkflow.lock_scope,
        ScopeLock(in_scope="in", out_of_scope="out"),
    )
    await _wait_snapshot(
        handle,
        lambda snap: snap.stage
        in {ParentStage.SCOPE_LOCKED, ParentStage.IMPLEMENTING},
    )
    await handle.signal(
        DevPipelineWorkflow.decide_scheme,
        SchemeDecision(has_high_risk_assumption=high_risk, tickets=list(payload.tickets)),
    )
    return await _wait_snapshot(
        handle,
        lambda snap: snap.stage
        in {ParentStage.IMPLEMENTING, ParentStage.SPIKE, ParentStage.BLOCKED},
    )


async def test_happy_path_reaches_done_via_test_deploying(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root, [TicketSpec(ticket_id="T-1")])
    controller = StubController()
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
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
    payload = _story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.hold("review", "B")
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
        snap = await _wait_snapshot(
            handle,
            lambda item: _stage_of(item, "A") is TicketStage.VERIFIED
            and _stage_of(item, "B") is TicketStage.REVIEWING,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        assert snap.stage not in {ParentStage.TEST_DEPLOYING, ParentStage.RETRO, ParentStage.DONE}
        controller.release("review", "B")
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_parent_cannot_enter_retro_when_test_deploy_fails(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    controller.publish_succeeds = False
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
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
    payload = _story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
        snap = await _wait_snapshot(
            handle,
            lambda item: _started(item, "A") and _started(item, "B"),
        )
        # Both must have been started without waiting on each other.
        assert {t.ticket_id for t in snap.tickets if t.started} == {"A", "B"}
        await _wait_until(lambda: set(controller.started) >= {"A", "B"})
        controller.release("implement", "A", "B")
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_dependent_ticket_does_not_start_early(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(
        tmp_config_root,
        [
            TicketSpec(ticket_id="A"),
            TicketSpec(ticket_id="B", depends_on=["A"]),
        ],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
        snap = await _wait_snapshot(handle, lambda item: _started(item, "A"))
        assert _started(snap, "A")
        assert not _started(snap, "B")
        assert _stage_of(snap, "B") is TicketStage.PENDING
        assert "B" not in controller.started
        controller.release("implement", "A")
        snap = await _wait_snapshot(
            handle, lambda item: _stage_of(item, "A") is TicketStage.VERIFIED
        )
        snap = await _wait_snapshot(handle, lambda item: _started(item, "B"))
        assert _started(snap, "B")
        controller.release("implement", "B")
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_one_child_failure_does_not_cancel_sibling_and_blocks_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(
        tmp_config_root,
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")],
    )
    controller = StubController()
    controller.implement_errors.add("A")
    controller.hold("implement", "B")
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
        snap = await _wait_snapshot(
            handle,
            lambda item: _stage_of(item, "A") is TicketStage.FAILED and _started(item, "B"),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        child_b = workflow_env.client.get_workflow_handle(
            ticket_workflow_id("B", payload.execution_id)
        )
        description = await child_b.describe()
        assert description.status.name not in {"CANCELED", "CANCELLED", "TERMINATED", "FAILED"}
        controller.release("implement", "B")
        snap = await _wait_snapshot(
            handle,
            lambda item: _stage_of(item, "B") is TicketStage.VERIFIED
            and _stage_of(item, "A") is TicketStage.FAILED,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        await handle.signal(DevPipelineWorkflow.drop_ticket, "A")
        result = await handle.result()
        assert result.stage is ParentStage.DONE
        assert result.test_deploy_status is TestDeployStatus.SUCCEEDED
    finally:
        await worker.__aexit__(None, None, None)


async def test_missing_config_root_fails_workflow_visibly(
    workflow_env: WorkflowEnvironment, tmp_path: Path
) -> None:
    payload = _story(tmp_path / "missing-config", [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        text = _exception_text(exc.value)
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
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        with pytest.raises(WorkflowFailureError) as exc:
            await handle.result()
        text = _exception_text(exc.value)
        assert "配置根目录" in text
        assert "必填" in text or "不能省略" in text
    finally:
        await worker.__aexit__(None, None, None)


async def test_production_publish_activity_is_rejected() -> None:
    env = ActivityEnvironment()
    with pytest.raises(ProductionDeployForbidden, match="禁止生产发布"):
        await env.run(
            publish_to_test_env,
            PublishRequest(story_id="s", execution_id="e", target="production"),
        )


async def test_blocking_review_waits_for_human_gate(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    controller.review_findings["A"] = ReviewFindings(blocking=["must gate"], paradigm_drift=False)
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await _advance_to_implementing(handle, payload)
        snap = await _wait_snapshot(
            handle, lambda item: _stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="A", decision="approve"),
        )
        result = await handle.result()
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_abort_from_intake_blocks_and_reopen_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.reopen, "exec-new")
        snap = await _wait_snapshot(handle, lambda item: item.last_illegal is not None)
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
    payload = _story(tmp_config_root, [TicketSpec(ticket_id="A")])
    controller = StubController()
    worker, handle = await _start(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, ScopeLock(in_scope="in"))
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            SchemeDecision(has_high_risk_assumption=True, tickets=list(payload.tickets)),
        )
        await _wait_snapshot(handle, lambda item: item.stage is ParentStage.SPIKE)
        await handle.signal(DevPipelineWorkflow.approve_spike, False)
        result = await handle.result()
        assert result.stage is ParentStage.BLOCKED
    finally:
        await worker.__aexit__(None, None, None)




async def _wait_until(predicate: object, *, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.05)
    raise AssertionError("timeout waiting for condition")


def _exception_text(exc: BaseException) -> str:
    chunks: list[str] = []
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chunks.append(str(current))
        message = getattr(current, "message", None)
        if message:
            chunks.append(str(message))
        current = getattr(current, "cause", None) or current.__cause__
    return "\n".join(chunks)


def _stage_of(snap: PipelineSnapshot, ticket_id: str) -> TicketStage | None:
    for item in snap.tickets:
        if item.ticket_id == ticket_id:
            return item.stage
    return None


def _started(snap: PipelineSnapshot, ticket_id: str) -> bool:
    for item in snap.tickets:
        if item.ticket_id == ticket_id:
            return item.started
    return False
