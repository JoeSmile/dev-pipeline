"""Live Temporal History proof for DP-27 Query, heartbeat, and waiting labels.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
Assertions query the snapshot; the workflow is aborted afterwards. Does not
await handle.result() or assert Temporal COMPLETED, and does not terminate an
already-completed workflow.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
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
from dev_pipeline.cli_status import query_pipeline_status
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import AbortRequest, story_workflow_id
from dev_pipeline.progress import UNRESPONSIVE_LABEL
from dev_pipeline.progress_log import load_recorded_progress
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow
from dev_pipeline.workspace import reset_default_claim_store

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
    client: Client,
    tmp_config_root: Path,
    controller: StubController,
) -> tuple[object, Worker, object]:
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    payload = story(tmp_config_root, [complete_ticket("T1")], suffix=suffix)
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-query-{suffix}"
    starter = ReopenStarter(client)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return payload, worker, handle


async def _finish(handle: object, worker: Worker) -> None:
    try:
        desc = await handle.describe()  # type: ignore[union-attr]
        if desc.status not in {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.FAILED,
        }:
            await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-cleanup"))
            try:
                await wait_terminal(handle, timeout=30.0)  # type: ignore[arg-type]
            except Exception:
                pass
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_query_fields_and_waiting_confirmation(
    live_client: Client, tmp_config_root: Path
) -> None:
    controller = StubController()
    payload, worker, handle = await _start_live(live_client, tmp_config_root, controller)
    try:
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: item.stage is ParentStage.INTAKE and item.budget is not None,
        )
        assert snap.waiting_signal == "lock_scope"
        assert snap.progress is not None
        assert snap.progress.waiting_confirmation == "正在等候你确认锁定范围"
        assert snap.budget is not None
        status = await query_pipeline_status(handle, config_root=str(tmp_config_root))
        assert status.stage is ParentStage.INTAKE
        assert status.progress is not None
        assert status.progress.unresponsive_label is None
        assert "正在等候你确认锁定范围" == status.progress.waiting_confirmation
        history = str(await handle.fetch_history())
        assert "CURSOR_API_KEY" not in history
        for comment in controller.jira.list_comments(payload.story_id):  # type: ignore[union-attr]
            assert "正在写代码" not in comment.body
    finally:
        await _finish(handle, worker)


async def test_live_in_flight_steps_and_heartbeat(
    live_client: Client, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.hold("implement", "T1")
    payload, worker, handle = await _start_live(live_client, tmp_config_root, controller)
    try:
        await advance_to_implementing(handle, payload)  # type: ignore[arg-type]
        await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: item.stage is ParentStage.IMPLEMENTING
            and any(ticket.started for ticket in item.tickets),
        )
        await wait_until(
            lambda: len(
                load_recorded_progress(
                    str(tmp_config_root), payload.execution_id  # type: ignore[union-attr]
                )["recent_hints"]
            )
            >= 2
        )
        recorded = load_recorded_progress(
            str(tmp_config_root), payload.execution_id  # type: ignore[union-attr]
        )
        assert len(recorded["recent_hints"]) >= 2
        status = await query_pipeline_status(handle, config_root=str(tmp_config_root))
        assert status.stage is ParentStage.IMPLEMENTING
        assert status.progress is not None
        assert status.progress.current_step
        assert status.progress.last_heartbeat_at
        assert status.progress.unresponsive_label is None
        assert len(status.progress.recent_hints) >= 2
        history = str(await handle.fetch_history())
        assert "CURSOR_API_KEY" not in history
    finally:
        controller.release("implement", "T1")
        await _finish(handle, worker)


async def test_live_expired_heartbeat_is_distinct_from_failed_and_waiting(
    live_client: Client, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.hold("implement", "T1")
    controller.pause_heartbeat.add("T1")
    payload, worker, handle = await _start_live(live_client, tmp_config_root, controller)
    try:
        await advance_to_implementing(handle, payload)  # type: ignore[arg-type]
        await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: item.stage is ParentStage.IMPLEMENTING
            and any(ticket.started for ticket in item.tickets),
        )
        await asyncio.sleep(1.2)
        status = await query_pipeline_status(
            handle,
            config_root=str(tmp_config_root),
            expiry=timedelta(seconds=1),
        )
        assert status.stage is ParentStage.IMPLEMENTING
        assert status.tickets[0].stage is not TicketStage.FAILED
        assert status.progress is not None
        assert status.progress.unresponsive_label == UNRESPONSIVE_LABEL
        assert status.progress.waiting_confirmation is None
        again = await handle.query(DevPipelineWorkflow.snapshot)
        assert again.stage is ParentStage.IMPLEMENTING
    finally:
        controller.release("implement", "T1")
        await _finish(handle, worker)
