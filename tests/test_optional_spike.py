"""WorkflowEnvironment tests for DP-18 optional Spike and approve_spike."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.history_events import (
    initiated_child_workflow_ids,
    scheduled_activity_names,
    started_child_workflow_ids,
)
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    scheme_decision,
    scope_lock,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.models import ApproveSpikeRequest, IntakeRecord, StoryInput, TicketSpec
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow

pytestmark = pytest.mark.asyncio


def _story(config_root: Path, *, auto_start_children: bool = False) -> StoryInput:
    payload = story(
        config_root,
        [TicketSpec(ticket_id="A")],
        auto_start_children=auto_start_children,
    )
    payload.intake = IntakeRecord(jira_key="DP-18", request="验证红线假设", source="cli")
    return payload


async def _lock_and_decide(
    handle: object,
    payload: StoryInput,
    *,
    high_risk: bool = True,
) -> None:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.lock_scope,
        scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
    )
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.SCOPE_LOCKED and item.waiting_signal == "decide_scheme"
        ),
    )
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.decide_scheme,
        scheme_decision(
            payload,
            high_risk=high_risk,
            decided_by="郭伟",
            tradeoff="先验证红线假设，再决定是否拆票",
        ),
    )


def _assert_no_coding_child(history: object, snap: object) -> None:
    assert initiated_child_workflow_ids(history) == []  # type: ignore[arg-type]
    assert started_child_workflow_ids(history) == []  # type: ignore[arg-type]
    assert started(snap, "A") is False  # type: ignore[arg-type]


async def test_verified_spike_reaches_tickets_ready_without_children(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload, high_risk=True)
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY and item.spike is not None,
        )
        assert snap.spike is not None
        assert snap.spike.conclusion == "验证过"
        assert "可能击穿 L0" in snap.spike.statement or snap.spike.statement
        assert snap.spike.timed_out is False
        assert snap.spike.budget_seconds > 0
        assert snap.spike.started_at
        assert snap.spike.finished_at
        assert snap.waiting_signal is None
        assert snap.stage is not ParentStage.IMPLEMENTING
        history = await handle.fetch_history()
        assert "run_spike" in scheduled_activity_names(history)
        assert "load_spike_config" in scheduled_activity_names(history)
        _assert_no_coding_child(history, snap)
        assert controller.spike_started == 1
    finally:
        await worker.__aexit__(None, None, None)


async def test_broken_red_line_without_protection_blocks(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = False
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_terminal(handle)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.spike is not None
        assert snap.spike.conclusion == "击穿红线"
        assert snap.spike.has_protection is False
        assert snap.blocked_reason is not None
        assert "无防护" in snap.blocked_reason
        history = await handle.fetch_history()
        assert "run_spike" in scheduled_activity_names(history)
        _assert_no_coding_child(history, snap)
    finally:
        await worker.__aexit__(None, None, None)


async def test_broken_red_line_with_protection_waits_for_approve_accept(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.SPIKE
                and item.waiting_signal == "approve_spike"
                and item.spike is not None
            ),
        )
        assert snap.spike is not None
        assert snap.spike.conclusion == "击穿红线"
        assert snap.spike.has_protection is True
        assert started(snap, "A") is False
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.TICKETS_READY)
        assert snap.stage is ParentStage.TICKETS_READY
        assert snap.waiting_signal is None
        history = await handle.fetch_history()
        _assert_no_coding_child(history, snap)
    finally:
        await worker.__aexit__(None, None, None)


async def test_broken_red_line_with_protection_reject_blocks(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SPIKE and item.waiting_signal == "approve_spike",
        )
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=False))
        snap = await wait_terminal(handle)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.blocked_reason is not None
        assert "拒绝" in snap.blocked_reason
        history = await handle.fetch_history()
        _assert_no_coding_child(history, snap)
    finally:
        await worker.__aexit__(None, None, None)


async def test_spike_overrun_is_unverified_and_blocks(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    (tmp_config_root / "spike.toml").write_text("timeout_seconds = 1\n", encoding="utf-8")
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_hang = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_terminal(handle, timeout=20.0)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.spike is not None
        assert snap.spike.conclusion == "超时未验证"
        assert snap.spike.timed_out is True
        assert snap.spike.has_protection is False
        assert snap.spike.budget_seconds == 1
        assert snap.blocked_reason is not None
        assert "超时未验证" in snap.blocked_reason
        history = await handle.fetch_history()
        assert "run_spike" in scheduled_activity_names(history)
        _assert_no_coding_child(history, snap)
    finally:
        await worker.__aexit__(None, None, None)


async def test_approve_spike_rejected_in_wrong_stage_and_after_verified(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        )
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "approve_spike"
            ),
        )
        assert snap.stage is ParentStage.INTAKE
        assert "当前不在 Spike" in (snap.last_rejection.reason if snap.last_rejection else "")
        await _lock_and_decide(handle, payload, high_risk=True)
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY and item.spike is not None,
        )
        assert snap.spike is not None
        assert snap.spike.conclusion == "验证过"
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "approve_spike"
                and item.stage is ParentStage.TICKETS_READY
            ),
        )
        assert snap.stage is ParentStage.TICKETS_READY
        assert "当前不在 Spike" in (snap.last_rejection.reason if snap.last_rejection else "")
    finally:
        await worker.__aexit__(None, None, None)


async def test_approve_spike_during_run_is_rejected_and_stage_stays_spike(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    (tmp_config_root / "spike.toml").write_text("timeout_seconds = 30\n", encoding="utf-8")
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_hang = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SPIKE and controller.spike_started >= 1,
        )
        assert snap.waiting_signal != "approve_spike"
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "approve_spike"
            ),
        )
        assert snap.stage is ParentStage.SPIKE
        reason = snap.last_rejection.reason if snap.last_rejection else ""
        assert "不在等候 approve_spike" in reason
    finally:
        await worker.__aexit__(None, None, None)


async def test_invalid_approve_spike_payload_is_rejected(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SPIKE and item.waiting_signal == "approve_spike",
        )
        await handle.signal(DevPipelineWorkflow.approve_spike, {"accepted": "yes"})
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "approve_spike"
            ),
        )
        assert snap.stage is ParentStage.SPIKE
        assert snap.waiting_signal == "approve_spike"
        assert "bool" in (snap.last_rejection.reason if snap.last_rejection else "")
    finally:
        await worker.__aexit__(None, None, None)


async def test_no_red_line_still_skips_spike_and_does_not_dispatch_children(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    worker, handle = await start_parent(workflow_env, payload, StubController())
    try:
        await _lock_and_decide(handle, payload, high_risk=False)
        snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.TICKETS_READY)
        assert snap.spike is None
        assert snap.waiting_signal is None
        history = await handle.fetch_history()
        assert "run_spike" not in scheduled_activity_names(history)
        _assert_no_coding_child(history, snap)
    finally:
        await worker.__aexit__(None, None, None)


async def test_query_snapshot_exposes_falsifiable_spike_conclusion(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = _story(tmp_config_root)
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = True
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await _lock_and_decide(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: item.spike is not None and item.waiting_signal == "approve_spike",
        )
        queried = await handle.query(DevPipelineWorkflow.snapshot)
        assert queried.spike is not None
        assert queried.spike.conclusion == "击穿红线"
        assert queried.spike.has_protection is True
        assert queried.spike.statement
        assert queried.spike.budget_seconds > 0
    finally:
        await worker.__aexit__(None, None, None)
