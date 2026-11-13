"""Live Temporal History proof for DP-30 abort recycle and reopen inheritance."""

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
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import AbortRequest, ReopenRequest, story_workflow_id
from dev_pipeline.remote_pr import reset_default_pr_client
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow
from dev_pipeline.workspace import reset_default_claim_store
from dev_pipeline.workspace_auth import derive_authorized_worktree

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


async def _start(
    live_client: Client,
    payload: object,
    controller: StubController,
) -> tuple[Worker, object]:
    task_queue = f"{DEFAULT_TASK_QUEUE}-cleanup-live-{uuid.uuid4().hex[:8]}"
    starter = ReopenStarter(live_client)
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
        id=story_workflow_id(payload.story_id, payload.execution_id),  # type: ignore[attr-defined]
        task_queue=task_queue,
    )
    return worker, handle


async def test_live_abort_recycles_workspace_and_history_remains(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    reset_default_pr_client()
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    payload = story(tmp_config_root, [complete_ticket("A")], suffix=uuid.uuid4().hex[:8])
    this_tree = Path(derive_authorized_worktree(str(authorized), "A", payload.execution_id))
    sibling = authorized / "A" / f"{payload.execution_id}-sibling"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "keep.txt").write_text("sibling", encoding="utf-8")
    worker, handle = await _start(live_client, payload, StubController())
    try:
        await advance_to_implementing(handle, payload)
        # ticket.started is set when the Child is spawned, before
        # claim_ticket_workspace creates the directory on disk.
        await wait_until(lambda: this_tree.is_dir(), timeout=30.0)
        assert this_tree.is_dir()
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-recycle"))
        snap = await wait_terminal(handle, timeout=30.0)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.abort_record is not None
        assert snap.abort_record.history_preserved is True
        assert not this_tree.exists()
        assert (sibling / "keep.txt").read_text(encoding="utf-8") == "sibling"
        history = await handle.fetch_history()
        assert "recycle_aborted_execution" in scheduled_activity_names(history)
        assert history.events
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
        reset_default_pr_client()


async def test_live_reopen_inherits_readonly_artifacts(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    payload = story(tmp_config_root, [complete_ticket("A")], suffix=uuid.uuid4().hex[:8])
    old_tree = Path(derive_authorized_worktree(str(authorized), "A", payload.execution_id))
    worker, handle = await _start(live_client, payload, StubController())
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: any(ticket.stage is TicketStage.VERIFIED for ticket in item.tickets),
            timeout=30.0,
        )
        assert snap.scope is not None
        assert snap.scheme is not None
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-reopen"))
        await wait_terminal(handle, timeout=30.0)
        new_id = f"{payload.execution_id}-reopen"
        new_tree = Path(derive_authorized_worktree(str(authorized), "A", new_id))
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        await wait_snapshot(
            handle, lambda item: item.reopened_execution_id == new_id, timeout=30.0
        )
        new_handle = live_client.get_workflow_handle(story_workflow_id(payload.story_id, new_id))
        new_snap = await wait_snapshot(
            new_handle,
            lambda item: item.inherited is not None and item.scheme is not None,
            timeout=30.0,
        )
        assert new_snap.execution_id == new_id
        assert new_snap.scope is not None
        assert new_snap.scope.in_scope == snap.scope.in_scope
        assert new_snap.scheme is not None
        assert new_snap.scheme.tradeoff == snap.scheme.tradeoff
        assert new_snap.inherited is not None
        assert [item.ticket_id for item in new_snap.inherited.closed_ticket_evidence] == ["A"]
        assert new_tree != old_tree
        assert not old_tree.exists()
        await new_handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-live-new"))
        await wait_terminal(new_handle, timeout=30.0)
        history = await handle.fetch_history()
        assert "start_reopened_workflow" in scheduled_activity_names(history)
        assert "recycle_aborted_execution" in scheduled_activity_names(history)
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
