"""Live Temporal History proof for DP-23 HumanGate decisions.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
If a child parks in HumanGate, the snapshot is queried and the workflow is
aborted or terminated after assertions. Does not terminate an already-completed
workflow.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path

import pytest
from temporalio.client import Client
from temporalio.worker import Worker
from tests.history_events import signaled_names
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    review_override,
    stage_of,
    story,
    wait_snapshot,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import ReviewFindings, story_workflow_id, ticket_workflow_id
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


async def _start_live_parent(
    client: Client,
    tmp_config_root: Path,
    setup: Callable[[StubController], None],
):
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{suffix}")
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
        suffix=suffix,
    )
    controller = StubController()
    setup(controller)
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-gate-{suffix}"
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


def _block_a(controller: StubController) -> None:
    controller.review_findings["A"] = ReviewFindings(blocking=["must gate"])


async def test_live_approve_records_decision_in_history(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    payload, worker, handle, _controller = await _start_live_parent(
        live_client, tmp_config_root, _block_a
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE,
            timeout=30.0,
        )
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "放行", decided_by="郭伟", reason="live-approve"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A")
            in {TicketStage.VERIFYING, TicketStage.VERIFIED},
            timeout=30.0,
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.action == "放行"
        assert ticket.review.override.decided_by == "郭伟"
        assert ticket.review.override.decided_at
        assert snap.stage is ParentStage.IMPLEMENTING
        history = await handle.fetch_history()
        assert "review_override" in signaled_names(history)
        assert "CURSOR_API_KEY" not in str(history)
        child = live_client.get_workflow_handle(
            ticket_workflow_id("A", payload.execution_id)
        )
        assert "review_override" in signaled_names(await child.fetch_history())
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "live-cleanup"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_rework_returns_only_that_ticket(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    payload, worker, handle, controller = await _start_live_parent(
        live_client, tmp_config_root, _block_a
    )
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.HUMAN_GATE
                and stage_of(item, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
            ),
            timeout=30.0,
        )
        sibling = stage_of(snap, "B")
        controller.hold("implement", "A")
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "打回", decided_by="郭伟"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.IMPLEMENTING,
            timeout=30.0,
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.action == "打回"
        assert stage_of(snap, "B") in {sibling, TicketStage.VERIFIED}
        assert snap.stage is ParentStage.IMPLEMENTING
        history = await handle.fetch_history()
        assert "review_override" in signaled_names(history)
        assert "CURSOR_API_KEY" not in str(history)
    finally:
        controller.release("implement", "A")
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "live-cleanup"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_reject_blocks_ticket_and_keeps_parent_implementing(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    payload, worker, handle, _controller = await _start_live_parent(
        live_client, tmp_config_root, _block_a
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.HUMAN_GATE,
            timeout=30.0,
        )
        await handle.signal(
            DevPipelineWorkflow.review_override,
            review_override("A", "否决", decided_by="郭伟"),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.BLOCKED,
            timeout=30.0,
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.unhandled_failure is True
        assert ticket.review is not None
        assert ticket.review.override is not None
        assert ticket.review.override.action == "否决"
        assert snap.stage is ParentStage.IMPLEMENTING
        history = await handle.fetch_history()
        assert "review_override" in signaled_names(history)
        assert "CURSOR_API_KEY" not in str(history)
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "live-cleanup"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
