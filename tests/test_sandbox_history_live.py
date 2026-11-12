"""Live Temporal History proof for DP-20 out-of-bounds sandbox rejection.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY.
"""

from __future__ import annotations

import asyncio
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
    complete_ticket,
    scheme_decision,
    scope_lock,
    story,
    wait_snapshot,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    IntakeRecord,
    SandboxWriteSpec,
    TicketInput,
    story_workflow_id,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]


async def _wait_child_snapshot(handle: object, *, timeout: float = 30.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = await handle.query(TicketWorkflow.snapshot)  # type: ignore[union-attr]
        if last.stage in {TicketStage.FAILED, TicketStage.VERIFIED}:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for child snapshot, last={last}")


SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


async def test_live_child_out_of_bounds_write_is_rejected_and_recorded(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    authorized = tmp_config_root.parent / f"authorized-{suffix}"
    write_workspace_toml(tmp_config_root, authorized)
    outside = tmp_config_root.parent / f"external-{suffix}" / "secret.txt"
    outside.parent.mkdir()
    payload = TicketInput(
        ticket_id="T1",
        execution_id=f"exec-live-sandbox-{suffix}",
        story_id=f"DP-20-{suffix}",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        sandbox_write=SandboxWriteSpec(path=str(outside), content="nope"),
    )
    controller = StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-sandbox-live-{suffix}"
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    try:
        handle = await live_client.start_workflow(
            TicketWorkflow.run,
            payload,
            id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
            task_queue=task_queue,
        )
        snap = await _wait_child_snapshot(handle)
        history = await handle.fetch_history()
        assert snap.stage is TicketStage.FAILED
        assert snap.sandbox is not None
        assert snap.sandbox.status == "rejected"
        assert snap.sandbox.reason
        assert not outside.exists()
        names = scheduled_activity_names(history)
        assert "apply_sandbox_write" in names
        assert "implement_ticket" not in names
        assert "agent" not in names
        await handle.terminate(reason="dp-20 live sandbox assertion complete")
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_parent_snapshot_records_out_of_bounds_write(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    authorized = tmp_config_root.parent / f"authorized-parent-{suffix}"
    write_workspace_toml(tmp_config_root, authorized)
    outside = tmp_config_root.parent / f"external-parent-{suffix}" / "secret.txt"
    outside.parent.mkdir()
    payload = story(
        tmp_config_root,
        [complete_ticket("A")],
        suffix=f"sandbox-{suffix}",
        auto_start_children=True,
    )
    payload.story_id = "DP-20"
    payload.intake = IntakeRecord(jira_key="DP-20", request="沙箱越界", source="cli")
    payload.sandbox_write = SandboxWriteSpec(path=str(outside), content="nope")
    controller = StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-sandbox-parent-live-{suffix}"
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
            lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
            timeout=30.0,
        )
        await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())
        await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.SCOPE_LOCKED,
            timeout=30.0,
        )
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.tickets
                and item.tickets[0].sandbox is not None
                and item.tickets[0].sandbox.status == "rejected"
            ),
            timeout=30.0,
        )
        record = snap.tickets[0].sandbox
        assert record is not None
        assert record.status == "rejected"
        assert snap.tickets[0].stage is TicketStage.FAILED
        assert not outside.exists()
        assert type(controller.jira).__name__ == "InMemoryJira"
        await handle.signal(
            DevPipelineWorkflow.abort,
            AbortRequest(reason="dp-20 live sandbox done"),
        )
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.BLOCKED, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)
