"""WorkflowEnvironment tests for DP-23 HumanGate and review_override."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import signaled_names
from tests.stub_activities import StubController
from tests.test_deliverables_workflow import _wait_child
from tests.test_review_workflow import _finish, _run_child
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    review_override,
    stage_of,
    start_parent,
    story,
    wait_snapshot,
    wait_until,
)

from dev_pipeline.models import ReviewFindings, TicketInput, ticket_workflow_id
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = pytest.mark.asyncio


def _ticket(tmp_config_root: Path, ticket_id: str, execution_id: str) -> TicketInput:
    return TicketInput(
        ticket_id=ticket_id,
        execution_id=execution_id,
        story_id=f"STORY-{execution_id}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )


async def _start_blocked_parent(
    env: WorkflowEnvironment,
    tmp_config_root: Path,
    *,
    blocking: list[str],
    sibling_blocking: list[str] | None = None,
    conflict: bool = False,
) -> tuple[object, object, object, StubController]:
    tickets = [complete_ticket("A"), complete_ticket("B")]
    payload = story(tmp_config_root, tickets)
    controller = StubController()
    if conflict:
        controller.review_findings_by_worker["A"] = {
            "standards": ReviewFindings(blocking=["L0 被改"]),
            "spec": ReviewFindings(blocking=["不得新增 forbidden.py"]),
        }
    else:
        controller.review_findings["A"] = ReviewFindings(blocking=blocking)
    if sibling_blocking is not None:
        controller.review_findings["B"] = ReviewFindings(blocking=sibling_blocking)
    worker, handle = await start_parent(env, payload, controller)
    await advance_to_implementing(handle, payload)
    return payload, worker, handle, controller


async def test_approve_moves_ticket_to_verifying_and_records_decision(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload, worker, handle, _controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["must gate"]
    )
    try:
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        assert stage_of(snap, "A") is TicketStage.HUMAN_GATE
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "放行", decided_by="郭伟", reason="可接受风险"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A")
            in {TicketStage.VERIFYING, TicketStage.VERIFIED},
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.decision == "approve"
        assert ticket.review.override.action == "放行"
        assert ticket.review.override.decided_by == "郭伟"
        assert ticket.review.override.reason == "可接受风险"
        assert ticket.review.override.decided_at
        assert ticket.review.auto_arbitrated is False
        if ticket.stage is TicketStage.VERIFYING:
            assert "人工放行" in ticket.detail
        assert snap.stage is ParentStage.IMPLEMENTING
        history = await handle.fetch_history()
        assert "review_override" in signaled_names(history)
        child_id = ticket_workflow_id("A", payload.execution_id)
        child = workflow_env.client.get_workflow_handle(child_id)
        child_history = await child.fetch_history()
        assert "review_override" in signaled_names(child_history)
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_rework_returns_only_that_ticket_to_implementing(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload, worker, handle, controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["must gate"]
    )
    try:
        snap = await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.HUMAN_GATE
                and stage_of(item, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
            ),
        )
        sibling_stage = stage_of(snap, "B")
        assert snap.scope is not None
        assert snap.scheme is not None
        locked_at = snap.scope.locked_at
        decided_at = snap.scheme.decided_at
        controller.hold("implement", "A")
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "打回", decided_by="郭伟", reason="先改契约"),
        )
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.IMPLEMENTING
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.decision == "rework"
        assert ticket.review.override.action == "打回"
        assert ticket.review.override.decided_by == "郭伟"
        assert "人工打回" in ticket.detail
        assert stage_of(snap, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert stage_of(snap, "B") in {sibling_stage, TicketStage.VERIFIED}
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.scope is not None and snap.scope.locked_at == locked_at
        assert snap.scheme is not None and snap.scheme.decided_at == decided_at
        await wait_until(lambda: controller.started.count("A") >= 2)
        assert stage_of(await handle.query(DevPipelineWorkflow.snapshot), "A") is (
            TicketStage.IMPLEMENTING
        )
    finally:
        controller.release("implement", "A")
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_reject_blocks_only_that_ticket_not_the_story(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _payload, worker, handle, _controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["must gate"]
    )
    try:
        await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.HUMAN_GATE
                and stage_of(item, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
            ),
        )
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "否决", decided_by="郭伟", reason="不可接受"),
        )
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.BLOCKED
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.unhandled_failure is True
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.decision == "reject"
        assert ticket.review.override.action == "否决"
        assert ticket.review.override.decided_by == "郭伟"
        assert "人工否决" in ticket.detail
        assert stage_of(snap, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.blocked_reason is None
        if stage_of(snap, "B") is TicketStage.VERIFIED:
            snap = await wait_snapshot(
                handle,
                lambda item: item.waiting_signal
                == "retry_ticket_or_drop_ticket_or_abort",
            )
            assert snap.stage is ParentStage.IMPLEMENTING
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_human_gate_waits_for_signal_and_does_not_auto_pass(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _payload, worker, handle, _controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["must gate"]
    )
    try:
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is None
        await asyncio.sleep(0.4)
        still = await handle.query(DevPipelineWorkflow.snapshot)
        assert stage_of(still, "A") is TicketStage.HUMAN_GATE
        ticket = next(item for item in still.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is None
        assert ticket.review.auto_arbitrated is False
        assert still.stage is ParentStage.IMPLEMENTING
        assert "review_override" not in signaled_names(await handle.fetch_history())
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_advice_only_skips_gate_and_rejects_review_override(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A")
            in {TicketStage.VERIFYING, TicketStage.VERIFIED},
        )
        before = stage_of(snap, "A")
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "放行", decided_by="tester"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "review_override"
                and "不在 HumanGate" in item.last_rejection.reason
            ),
        )
        assert stage_of(snap, "A") == before
        assert snap.stage is ParentStage.IMPLEMENTING
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.requires_human_gate is False
        assert ticket.review.override is None
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_invalid_or_wrong_stage_override_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _payload, worker, handle, _controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["must gate"]
    )
    try:
        await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        await handle.signal(
            DevPipelineWorkflow.review_override,
            {"ticket_id": "A", "decision": "maybe", "decided_by": "tester"},
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and "放行、打回、否决" in item.last_rejection.reason
            ),
        )
        assert stage_of(snap, "A") is TicketStage.HUMAN_GATE
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(
            DevPipelineWorkflow.review_override,
            {"ticket_id": "A", "decision": "approve"},
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and "缺少必填字段" in item.last_rejection.reason
                and "decided_by" in item.last_rejection.reason
            ),
        )
        assert stage_of(snap, "A") is TicketStage.HUMAN_GATE
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_conflicting_reviews_still_wait_without_auto_arbitration(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _payload, worker, handle, _controller = await _start_blocked_parent(
        workflow_env, tmp_config_root, blocking=["unused"], conflict=True
    )
    try:
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.conflicting_blocks is True
        assert ticket.review.auto_arbitrated is False
        await asyncio.sleep(0.3)
        still = await handle.query(DevPipelineWorkflow.snapshot)
        ticket = next(item for item in still.tickets if item.ticket_id == "A")
        assert ticket.stage is TicketStage.HUMAN_GATE
        assert ticket.review is not None
        assert ticket.review.auto_arbitrated is False
        assert ticket.review.override is None
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)


async def test_stale_child_override_before_gate_is_ignored(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.hold("review", "T-stale")
    controller.review_findings["T-stale"] = ReviewFindings(blocking=["must gate"])
    payload = _ticket(tmp_config_root, "T-stale", "exec-stale")
    handle, worker = await _run_child(
        workflow_env, payload, list(controller.activities())
    )
    try:
        snap = await _wait_child(
            handle,
            stages=frozenset({TicketStage.IMPLEMENTING, TicketStage.REVIEWING}),
        )
        assert snap.stage in {TicketStage.IMPLEMENTING, TicketStage.REVIEWING}
        await handle.signal(
            TicketWorkflow.review_override,
            review_override("T-stale", "放行", decided_by="tester"),
        )
        controller.release("review", "T-stale")
        snap = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
        assert snap.stage is TicketStage.HUMAN_GATE
        await asyncio.sleep(0.2)
        still = await handle.query(TicketWorkflow.snapshot)
        assert still.stage is TicketStage.HUMAN_GATE
        assert still.review is not None
        assert still.review.override is None
    finally:
        await _finish(handle, worker)
