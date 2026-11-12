"""Live Temporal History proof for DP-16 lock_scope.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
"""

from __future__ import annotations

import os
import re
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
)
from tests.stub_activities import StubController
from tests.workflow_helpers import scope_lock, wait_snapshot

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import IntakeRecord, StoryInput, TicketSpec, story_workflow_id
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"
_LOCKED_AT = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def _start_live(
    live_client: Client, tmp_config_root: Path, suffix: str
) -> tuple[Worker, object, StoryInput]:
    payload = StoryInput(
        story_id="DP-16",
        execution_id=f"exec-live-lock-{suffix}",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="不要用这段",
        intake=IntakeRecord(
            jira_key="DP-16",
            request="把登录改成单点登录",
            source="cli",
        ),
    )
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-lock-live-{suffix}"
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
    return worker, handle, payload


async def test_live_lock_scope_writes_record_and_does_not_dispatch_child(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, _payload = await _start_live(live_client, tmp_config_root, f"ok-{token}")
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            scope_lock(in_scope="登录单点", out_of_scope="不改计费", locked_by="郭伟"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED
            and item.waiting_signal == "decide_scheme",
            timeout=30.0,
        )
        assert snap.scope is not None
        assert snap.scope.in_scope == "登录单点"
        assert snap.scope.out_of_scope == "不改计费"
        assert snap.scope.locked_by == "郭伟"
        assert _LOCKED_AT.match(snap.scope.locked_at)
        assert all(not item.started for item in snap.tickets)
        history = await handle.fetch_history()
        assert "lock_scope" in signaled_names(history)
        assert "normalize_story_draft" in scheduled_activity_names(history)
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_lock_scope_rejects_missing_fields_and_stays_intake(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    worker, handle, _payload = await _start_live(live_client, tmp_config_root, f"rej-{token}")
    try:
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await handle.signal(DevPipelineWorkflow.lock_scope, {"in_scope": "只要解决"})
        snap = await wait_snapshot(
            handle,
            lambda item: item.last_rejection is not None
            and item.last_rejection.signal == "lock_scope",
            timeout=30.0,
        )
        assert snap.stage is ParentStage.INTAKE
        assert snap.scope is None
        assert snap.waiting_signal == "lock_scope"
        assert "必填" in (snap.last_rejection.reason if snap.last_rejection else "")
        history = await handle.fetch_history()
        assert "lock_scope" in signaled_names(history)
        assert initiated_child_workflow_ids(history) == []
    finally:
        await worker.__aexit__(None, None, None)
