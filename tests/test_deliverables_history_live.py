"""Live Temporal History proof for DP-21 ticket deliverables.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.test_deliverables_workflow import _real_implement_activities, _wait_child

from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import TicketInput, ticket_workflow_id
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows import TicketWorkflow
from dev_pipeline.workspace import derive_branch_name, reset_default_claim_store

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


async def test_live_complete_deliverables_enter_reviewing(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    authorized = tmp_config_root.parent / f"authorized-{suffix}"
    write_workspace_toml(tmp_config_root, authorized)
    controller = StubController()
    controller.hold("review", "T1")
    payload = TicketInput(
        ticket_id="T1",
        execution_id=f"exec-live-{suffix}",
        story_id=f"STORY-live-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-deliv-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=_real_implement_activities(controller),
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        snap = await _wait_child(handle, timeout=30.0)
        assert snap.stage is TicketStage.REVIEWING
        assert snap.deliverables is not None
        assert snap.deliverables.complete is True
        assert snap.deliverables.branch_name == derive_branch_name(
            payload.ticket_id, payload.execution_id
        )
        assert snap.deliverables.tests_ran_in_worktree is True
        assert str(authorized.resolve()) in snap.deliverables.worktree or (
            payload.ticket_id in snap.deliverables.worktree
        )
        assert snap.deliverables.remote_pr_opened is False
        assert snap.deliverables.pushed is False
        assert snap.deliverables.merged is False
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert "implement_ticket" in names
        # review_ticket may already be scheduled while the child is parked
        # in Reviewing behind the test gate; it must not have moved on.
        assert snap.stage is TicketStage.REVIEWING
        text = str(history)
        assert "CURSOR_API_KEY" not in text
        assert "super-secret" not in text
        assert "--api-key" not in text
    finally:
        await handle.terminate()
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_unauthorized_path_does_not_push_or_open_pr(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    # No workspace.toml: claim stays unauthorized, deliverables stay incomplete.
    payload = TicketInput(
        ticket_id="T2",
        execution_id=f"exec-unauth-{suffix}",
        story_id=f"STORY-unauth-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    controller = StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-unauth-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=_real_implement_activities(controller),
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        snap = await _wait_child(handle, timeout=30.0)
        assert snap.stage is TicketStage.FAILED
        assert snap.deliverables is not None
        assert snap.deliverables.complete is False
        assert snap.deliverables.pushed is False
        assert snap.deliverables.merged is False
        assert snap.deliverables.remote_pr_opened is False
        assert snap.deliverables.pr_authorization == "待证"
        assert "review_ticket" not in scheduled_activity_names(await handle.fetch_history())
    finally:
        await handle.terminate()
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_incomplete_stub_does_not_enter_reviewing(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    controller = StubController()
    controller.implement_incomplete.add("T-miss")
    payload = TicketInput(
        ticket_id="T-miss",
        execution_id=f"exec-miss-{suffix}",
        story_id=f"STORY-miss-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-miss-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        snap = await _wait_child(handle, timeout=30.0)
        assert snap.stage is TicketStage.FAILED
        assert "交付物不齐" in snap.detail
        assert "review_ticket" not in scheduled_activity_names(await handle.fetch_history())
    finally:
        await handle.terminate()
        await worker.__aexit__(None, None, None)
