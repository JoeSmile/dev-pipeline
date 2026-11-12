"""History proof against a real Temporal server. Skipped unless TEMPORAL_TARGET is set."""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import scheme_decision, story, wait_snapshot, wait_terminal

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    ApproveSpikeRequest,
    PromoteRuleRequest,
    ReopenRequest,
    ReviewOverride,
    ScopeLock,
    TicketSpec,
    story_workflow_id,
)
from dev_pipeline.signal_schemas import HUMAN_SIGNAL_NAMES
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


async def test_live_temporal_history_records_signals_and_reopen(
    live_client: Client, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")], suffix=uuid.uuid4().hex[:8])
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    try:
        handle = await live_client.start_workflow(
            DevPipelineWorkflow.run,
            payload,
            id=story_workflow_id(payload.story_id, payload.execution_id),
            task_queue=task_queue,
        )
        await handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(payload),
        )
        await handle.signal(DevPipelineWorkflow.approve_spike, ApproveSpikeRequest(accepted=True))
        await handle.signal(
            DevPipelineWorkflow.review_override,
            ReviewOverride(ticket_id="A", decision="approve"),
        )
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            PromoteRuleRequest(rule_name="skill", automatic=False),
        )
        await handle.signal(DevPipelineWorkflow.authorize_test_deploy)
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id="too-early"))
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
        )
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-abort"))
        await wait_terminal(handle, timeout=30.0)
        new_id = f"{payload.execution_id}-live"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(
            handle, lambda item: item.reopened_execution_id == new_id, timeout=30.0
        )
        assert snap.stage is ParentStage.BLOCKED
        await handle.result()
        history = await handle.fetch_history()
        names = signaled_names(history)
        missing = [name for name in HUMAN_SIGNAL_NAMES if name not in names]
        assert missing == [], f"Live History 缺少 Signal: {missing}; 实际={names}"
        assert "start_reopened_workflow" in scheduled_activity_names(history)
        new_handle = live_client.get_workflow_handle(story_workflow_id(payload.story_id, new_id))
        new_snap = await wait_snapshot(
            new_handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED, timeout=30.0
        )
        assert new_snap.execution_id == new_id
        assert new_snap.scope is not None
        assert new_snap.scope.in_scope == "in"
        assert new_snap.scope.out_of_scope == "out"
        assert new_snap.scope.locked_by == "tester"
        assert new_snap.scope.locked_at
    finally:
        await worker.__aexit__(None, None, None)
