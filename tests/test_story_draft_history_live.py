"""Live Temporal History proof for DP-15 Story draft normalization.

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
from tests.history_events import scheduled_activity_names, signaled_names
from tests.stub_activities import StubController
from tests.workflow_helpers import wait_snapshot

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import IntakeRecord, StoryInput, story_workflow_id
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


async def test_live_intake_records_story_draft_without_locking_scope(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    payload = StoryInput(
        story_id="DP-15",
        execution_id=f"exec-live-draft-{token}",
        config_root=str(tmp_config_root),
        tickets=[],
        summary="不要用这段",
        intake=IntakeRecord(
            jira_key="DP-15",
            request="把登录改成单点登录",
            source="cli",
        ),
    )
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-draft-live-{token}"
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
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        assert snap.waiting_signal == "lock_scope"
        assert snap.story_draft is not None
        assert snap.story_draft.to_solve == "把登录改成单点登录"
        assert snap.story_draft.known_constraints == "待证"
        assert snap.story_draft.acceptance == "待证"
        assert "已知约束: 待证" in snap.story_draft.missing_items
        assert "验收: 待证" in snap.story_draft.missing_items
        assert snap.story_draft.complete is False
        history = await handle.fetch_history()
        assert "normalize_story_draft" in scheduled_activity_names(history)
        assert "lock_scope" not in signaled_names(history)
        assert snap.stage is ParentStage.INTAKE
    finally:
        await worker.__aexit__(None, None, None)
