"""Live Temporal History proof for DP-19 ticket list and child dispatch.

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
from tests.history_events import initiated_child_workflow_ids, scheduled_activity_names
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    complete_ticket,
    scheme_decision,
    scope_lock,
    started,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    IntakeRecord,
    StoryInput,
    TicketListSubmission,
    TicketSpec,
    story_workflow_id,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TicketStage
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
    payload: StoryInput,
    controller: StubController | None = None,
) -> tuple[Worker, object, StubController]:
    controller = controller or StubController()
    starter = ReopenStarter(live_client)
    task_queue = f"{DEFAULT_TASK_QUEUE}-tickets-live-{suffix}"
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
    return worker, handle, controller


def _payload(
    tmp_config_root: Path,
    suffix: str,
    tickets: list[TicketSpec],
    *,
    auto_start_children: bool = False,
) -> StoryInput:
    return StoryInput(
        story_id="DP-19",
        execution_id=f"exec-live-tickets-{suffix}",
        config_root=str(tmp_config_root),
        tickets=tickets,
        summary="拆票清单",
        intake=IntakeRecord(jira_key="DP-19", request="拆票清单", source="cli"),
        auto_start_children=auto_start_children,
    )



async def _lock_and_decide(handle: object, payload: StoryInput) -> None:
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
        timeout=30.0,
    )
    await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())  # type: ignore[attr-defined]
    await wait_snapshot(
        handle,  # type: ignore[arg-type]
        lambda item: (
            item.stage is ParentStage.SCOPE_LOCKED and item.waiting_signal == "decide_scheme"
        ),
        timeout=30.0,
    )
    await handle.signal(  # type: ignore[attr-defined]
        DevPipelineWorkflow.decide_scheme,
        scheme_decision(payload),
    )


async def test_live_complete_list_creates_children(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    payload = _payload(
        tmp_config_root,
        suffix,
        [complete_ticket("A"), complete_ticket("B")],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle, controller = await _start_live(
        live_client, tmp_config_root, suffix, payload, controller
    )
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.IMPLEMENTING
                and all(ticket.child_created for ticket in item.tickets)
                and started(item, "A")
                and started(item, "B")
            ),
            timeout=30.0,
        )
        assert snap.ticket_list_complete is True
        assert started(snap, "A") and started(snap, "B")
        assert all(ticket.jira_key for ticket in snap.tickets)
        history = await handle.fetch_history()
        assert set(initiated_child_workflow_ids(history)) == {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        assert "persist_jira_tickets" in scheduled_activity_names(history)
        assert controller.jira.issues()
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop live complete"))
        await wait_terminal(handle, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_incomplete_list_does_not_dispatch_children(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    payload = _payload(
        tmp_config_root,
        suffix,
        [TicketSpec(ticket_id="A")],
        auto_start_children=True,
    )
    worker, handle, controller = await _start_live(live_client, tmp_config_root, suffix, payload)
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.TICKETS_READY and item.waiting_signal == "submit_tickets"
            ),
            timeout=30.0,
        )
        assert snap.ticket_list_complete is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert "persist_jira_tickets" not in scheduled_activity_names(history)
        assert not controller.started
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop live incomplete"))
        await wait_terminal(handle, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_unmet_deps_child_stays_pending(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    payload = _payload(
        tmp_config_root,
        suffix,
        [complete_ticket("A"), complete_ticket("B", depends_on=["A"])],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle, controller = await _start_live(
        live_client, tmp_config_root, suffix, payload, controller
    )
    try:
        await _lock_and_decide(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                started(item, "A")
                and any(ticket.ticket_id == "B" and ticket.child_created for ticket in item.tickets)
            ),
            timeout=30.0,
        )
        by_id = {item.ticket_id: item for item in snap.tickets}
        assert by_id["B"].stage is TicketStage.PENDING
        assert not by_id["B"].started
        assert "B" not in controller.started
        history = await handle.fetch_history()
        assert set(initiated_child_workflow_ids(history)) == {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop live deps"))
        await wait_terminal(handle, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)


async def test_live_submit_tickets_then_children(
    live_client: Client, tmp_config_root: Path
) -> None:
    suffix = uuid.uuid4().hex[:8]
    payload = _payload(tmp_config_root, suffix, [])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle, controller = await _start_live(
        live_client, tmp_config_root, suffix, payload, controller
    )
    try:
        await _lock_and_decide(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: item.waiting_signal == "submit_tickets",
            timeout=30.0,
        )
        await handle.signal(
            DevPipelineWorkflow.submit_tickets,
            TicketListSubmission(tickets=[complete_ticket("A")], submitted_by="tester"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.IMPLEMENTING and started(item, "A"),
            timeout=30.0,
        )
        assert snap.ticket_list_complete is True
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == [
            ticket_workflow_id("A", payload.execution_id)
        ]
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop live submit"))
        await wait_terminal(handle, timeout=30.0)
    finally:
        await worker.__aexit__(None, None, None)
