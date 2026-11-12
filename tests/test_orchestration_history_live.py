"""Live Temporal History proof for DP-26 parent/child orchestration.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
Assertions query the snapshot; the workflow is aborted afterwards. Does not
await handle.result() or assert Temporal COMPLETED, and does not terminate an
already-completed workflow.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
from tests.history_events import initiated_child_workflow_ids, started_child_workflow_ids
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    stage_of,
    started,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import AbortRequest, story_workflow_id, ticket_workflow_id
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
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
    tickets: list[object],
    controller: StubController,
) -> tuple[object, Worker, object, StubController]:
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    payload = story(tmp_config_root, tickets, suffix=suffix)  # type: ignore[arg-type]
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-orch-{suffix}"
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
    return payload, worker, handle, controller


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
    except Exception:
        pass
    await worker.__aexit__(None, None, None)
    reset_default_claim_store()


def _not_cancelled(status_name: str) -> bool:
    return status_name not in {"CANCELED", "CANCELLED", "TERMINATED", "FAILED"}


async def test_live_independent_tickets_start_in_parallel(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    controller = StubController()
    controller.hold("implement", "A", "B")
    payload, worker, handle, controller = await _start_live(
        live_client,
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
        controller,
    )
    try:
        await advance_to_implementing(handle, payload)  # type: ignore[arg-type]
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: started(item, "A") and started(item, "B"),
            timeout=30.0,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert {item.ticket_id for item in snap.tickets if item.child_created} == {"A", "B"}
        await wait_until(lambda: set(controller.started) >= {"A", "B"}, timeout=30.0)
        history = await handle.fetch_history()  # type: ignore[union-attr]
        expected = {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        assert expected <= set(initiated_child_workflow_ids(history))
        assert expected <= set(started_child_workflow_ids(history))
        assert handle.id == story_workflow_id(payload.story_id, payload.execution_id)  # type: ignore[union-attr]
        assert "CURSOR_API_KEY" not in str(history)
        for ticket_id in ("A", "B"):
            child = live_client.get_workflow_handle(
                ticket_workflow_id(ticket_id, payload.execution_id)
            )
            description = await child.describe()
            assert _not_cancelled(description.status.name)
    finally:
        controller.release("implement", "A", "B")
        await _finish(handle, worker)


async def test_live_dependent_ticket_stays_pending_until_deps_verified(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    controller = StubController()
    controller.hold("implement", "A", "B")
    payload, worker, handle, controller = await _start_live(
        live_client,
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B", depends_on=["A"])],
        controller,
    )
    try:
        await advance_to_implementing(handle, payload)  # type: ignore[arg-type]
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: started(item, "A") and item.tickets[-1].child_created,
            timeout=30.0,
        )
        assert started(snap, "A")
        assert not started(snap, "B")
        assert stage_of(snap, "B") is TicketStage.PENDING
        assert "B" not in controller.started
        history = await handle.fetch_history()  # type: ignore[union-attr]
        expected = {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        assert expected <= set(initiated_child_workflow_ids(history))
        controller.release("implement", "A")
        await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: stage_of(item, "A") is TicketStage.VERIFIED,
            timeout=30.0,
        )
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: started(item, "B"),
            timeout=30.0,
        )
        assert started(snap, "B")
        assert snap.stage is ParentStage.IMPLEMENTING
        assert "CURSOR_API_KEY" not in str(await handle.fetch_history())  # type: ignore[union-attr]
    finally:
        controller.release("implement", "A", "B")
        await _finish(handle, worker)


async def test_live_one_child_failure_does_not_cancel_sibling_or_enter_retro(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    controller = StubController()
    controller.implement_errors.add("A")
    controller.hold("implement", "B")
    payload, worker, handle, controller = await _start_live(
        live_client,
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
        controller,
    )
    try:
        await advance_to_implementing(handle, payload)  # type: ignore[arg-type]
        await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: stage_of(item, "A") is TicketStage.FAILED and started(item, "B"),
            timeout=30.0,
        )
        child_b = live_client.get_workflow_handle(
            ticket_workflow_id("B", payload.execution_id)
        )
        description = await child_b.describe()
        assert _not_cancelled(description.status.name)
        controller.release("implement", "B")
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: (
                stage_of(item, "B") is TicketStage.VERIFIED
                and stage_of(item, "A") is TicketStage.FAILED
            ),
            timeout=30.0,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        assert snap.waiting_signal == "retry_ticket_or_drop_ticket_or_abort"
        await handle.signal(DevPipelineWorkflow.authorize_test_deploy)  # type: ignore[attr-defined]
        snap = await wait_snapshot(
            handle,  # type: ignore[arg-type]
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "authorize_test_deploy"
            ),
            timeout=30.0,
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage not in {ParentStage.TEST_DEPLOYING, ParentStage.RETRO, ParentStage.DONE}
        assert "失败尚未处理" in (snap.last_rejection.reason if snap.last_rejection else "")
        later = await child_b.describe()
        assert later.status.name not in {"CANCELED", "CANCELLED", "TERMINATED"}
        history = await handle.fetch_history()  # type: ignore[union-attr]
        assert "CURSOR_API_KEY" not in str(history)
    finally:
        controller.release("implement", "B")
        await _finish(handle, worker)
