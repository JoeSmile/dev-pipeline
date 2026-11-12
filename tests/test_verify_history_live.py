"""Live Temporal History proof for DP-24 Verifying pass and fail-back.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue.
If a child parks in Implementing after a failed verify, the snapshot is queried
and the workflow is aborted after assertions. Does not terminate an already
completed workflow.
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
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    stage_of,
    story,
    wait_snapshot,
    wait_until,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.activities.sandbox import apply_sandbox_write
from dev_pipeline.activities.verify import verify_ticket
from dev_pipeline.activities.workspace import claim_ticket_workspace
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.models import story_workflow_id, ticket_workflow_id
from dev_pipeline.review import MAX_FILE_LINES
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


def _real_verify_activities(controller: StubController) -> list[object]:
    skip = {"verify_ticket", "claim_ticket_workspace", "apply_sandbox_write"}
    kept = [item for item in controller.activities() if getattr(item, "__name__", "") not in skip]
    kept.extend([claim_ticket_workspace, apply_sandbox_write, verify_ticket])
    return kept


async def _start_live(
    client: Client,
    tmp_config_root: Path,
    *,
    suffix: str,
    plant_huge_on: str | None = None,
):
    authorized = tmp_config_root.parent / f"authorized-live-{suffix}"
    write_workspace_toml(tmp_config_root, authorized)
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
        suffix=suffix,
    )
    if plant_huge_on:
        tree = authorized / plant_huge_on / payload.execution_id
        tree.mkdir(parents=True, exist_ok=True)
        (tree / "huge.py").write_text(
            "\n".join(f"line-{i}" for i in range(MAX_FILE_LINES + 1)),
            encoding="utf-8",
        )
    controller = StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-verify-{suffix}"
    starter = ReopenStarter(client)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*_real_verify_activities(controller), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    return worker, handle, payload, controller


async def test_live_verify_pass_writes_evidence_and_history(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload, _controller = await _start_live(
        live_client, tmp_config_root, suffix=f"ok-{suffix}"
    )
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.VERIFIED
                and stage_of(item, "B") is TicketStage.VERIFIED
            ),
        )
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.verify is not None
        assert ticket.verify.passed is True
        assert ticket.verify.evidence_path
        assert Path(ticket.verify.evidence_path).is_file()
        text = Path(ticket.verify.evidence_path).read_text(encoding="utf-8")
        assert "对应红线" in text
        assert "命令或现象" in text
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage is not ParentStage.RETRO
        child = live_client.get_workflow_handle(ticket_workflow_id("A", payload.execution_id))
        history = await child.fetch_history()
        assert "verify_ticket" in scheduled_activity_names(history)
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "live-verify-pass-stop"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_live_verify_failure_returns_only_that_ticket(
    live_client: Client, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = uuid.uuid4().hex[:8]
    worker, handle, payload, controller = await _start_live(
        live_client, tmp_config_root, suffix=f"fail-{suffix}", plant_huge_on="A"
    )
    controller.hold("review", "A")
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: stage_of(item, "A") is TicketStage.REVIEWING)
        controller.hold("implement", "A")
        controller.release("review", "A")
        await wait_snapshot(handle, lambda item: stage_of(item, "A") is TicketStage.IMPLEMENTING)
        await wait_until(lambda: controller.started.count("A") >= 2)
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert stage_of(snap, "A") is TicketStage.IMPLEMENTING
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.verify is not None
        assert ticket.verify.passed is False
        assert Path(ticket.verify.evidence_path).is_file()
        assert stage_of(snap, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage is not ParentStage.RETRO
        child = live_client.get_workflow_handle(ticket_workflow_id("A", payload.execution_id))
        history = await child.fetch_history()
        assert "verify_ticket" in scheduled_activity_names(history)
    finally:
        controller.release("implement", "A")
        controller.release("review", "A")
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "live-verify-fail-stop"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()
