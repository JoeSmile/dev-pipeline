"""Live Temporal History proof for DP-14 Jira stage mirror.

Uses the in-memory Jira fake; it does not comment on a real issue.
Skipped unless TEMPORAL_TARGET is set. Prefer an already-running server
(for example host.docker.internal:7233) instead of binding another 7233.

A real Jira fixture test is skipped unless both a disposable issue key and
a config-root with jira.toml are provided. DP-14 itself is never used.
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
from dev_pipeline.jira_mirror import STAGE_MIRROR_PREFIX
from dev_pipeline.models import (
    IntakeRecord,
    ScopeLock,
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
_FIXTURE_KEY = "DP-9999"
@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def test_live_stage_change_schedules_mirror_activity_and_history_stays_truth(
    live_client: Client, tmp_config_root: Path
) -> None:
    token = uuid.uuid4().hex[:8]
    payload = StoryInput(
        story_id=_FIXTURE_KEY,
        execution_id=f"exec-live-{token}",
        config_root=str(tmp_config_root),
        tickets=[TicketSpec(ticket_id="A")],
        summary="jira-mirror-live",
        intake=IntakeRecord(jira_key=_FIXTURE_KEY, source="cli"),
    )
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-jira-live-{token}"
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
        await wait_snapshot(
            handle,
            lambda snap: snap.stage is ParentStage.INTAKE
            and snap.jira_mirror is not None
            and snap.jira_mirror.last_mirrored_stage == "Intake",
            timeout=30.0,
        )
        await handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED
            and item.jira_mirror is not None
            and item.jira_mirror.last_mirrored_stage == "ScopeLocked"
            and item.jira_mirror.lagged is False,
            timeout=30.0,
        )
        assert controller.jira.current_stage_text(_FIXTURE_KEY) == "ScopeLocked"
        comments = controller.jira.comments(_FIXTURE_KEY)
        assert len(comments) == 1
        assert comments[0].body.startswith(STAGE_MIRROR_PREFIX)
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert names.count("mirror_parent_stage") >= 2
        assert all("jira" not in name.lower() for name in signaled_names(history))
        assert snap.stage is ParentStage.SCOPE_LOCKED
    finally:
        await worker.__aexit__(None, None, None)

