"""Live Temporal History proof for DP-18 optional Spike branches.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import (
    initiated_child_workflow_ids,
    scheduled_activity_names,
    signaled_names,
    started_child_workflow_ids,
)
from tests.stub_activities import StubController
from tests.workflow_helpers import scheme_decision, scope_lock, wait_snapshot, wait_terminal

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    ApproveSpikeRequest,
    IntakeRecord,
    StoryInput,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def _start_live(
    live_client: Client,
    tmp_config_root: Path,
    suffix: str,
    controller: StubController | None = None,
) -> tuple[Worker, object, StoryInput, StubController]:
    payload = StoryInput(
        story_id="DP-18",
        execution_id=f"exec-live-spike-{suffix}",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="验证红线假设",
        intake=IntakeRecord(
            jira_key="DP-18",
            request="验证红线假设",
            source="cli",
        ),
        auto_start_children=False,
    )
    controller = controller or StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-spike-live-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload, controller


async def _lock_and_decide(handle: object, payload: StoryInput) -> None:
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.lock_scope,
        scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
    )
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.SCOPE_LOCKED and item.waiting_signal == "decide_scheme"
        ),
        timeout=30.0,
    )
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.decide_scheme,
        scheme_decision(
            payload,
            high_risk=True,
            decided_by="郭伟",
            tradeoff="先验证红线假设，再决定是否拆票",
        ),
    )


def _assert_no_child(history: object) -> None:
    assert initiated_child_workflow_ids(history) == []  # type: ignore[arg-type]
    assert started_child_workflow_ids(history) == []  # type: ignore[arg-type]


async def test_live_verified_spike_reaches_tickets_ready(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, payload, _controller = await _start_live(
        live_client, tmp_config_root, f"ok-{token}"
    )
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY and item.spike is not None,
            timeout=30.0,
        )
        assert snap.spike is not None
        assert snap.spike.conclusion == "验证过"
        assert snap.waiting_signal is None
        assert all(not item.started for item in snap.tickets)
        history = await handle.fetch_history()
        assert "decide_scheme" in signaled_names(history)
        assert "run_spike" in scheduled_activity_names(history)
        _assert_no_child(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_broken_red_line_without_protection_blocks(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = False
    worker, handle, payload, _controller = await _start_live(
        live_client, tmp_config_root, f"fail-{token}", controller
    )
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_decide(handle, payload)
        snap = await wait_terminal(handle, timeout=30.0)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.spike is not None
        assert snap.spike.conclusion == "击穿红线"
        assert snap.spike.has_protection is False
        assert snap.blocked_reason is not None
        assert "无防护" in snap.blocked_reason
        history = await handle.fetch_history()
        assert "run_spike" in scheduled_activity_names(history)
        _assert_no_child(history)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_broken_red_line_with_protection_approve_reaches_tickets_ready(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    controller = StubController()
    controller.spike_red_line = True
    controller.spike_has_protection = True
    worker, handle, payload, _controller = await _start_live(
        live_client, tmp_config_root, f"prot-{token}", controller
    )
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.SPIKE
                and item.waiting_signal == "approve_spike"
                and item.spike is not None
            ),
            timeout=30.0,
        )
        assert snap.spike is not None
        assert snap.spike.conclusion == "击穿红线"
        assert snap.spike.has_protection is True
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.TICKETS_READY,
            timeout=30.0,
        )
        assert snap.waiting_signal is None
        assert all(not item.started for item in snap.tickets)
        history = await handle.fetch_history()
        assert "approve_spike" in signaled_names(history)
        assert "run_spike" in scheduled_activity_names(history)
        _assert_no_child(history)
    finally:
        await worker.__aexit__(None, None, None)
