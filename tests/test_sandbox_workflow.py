"""WorkflowEnvironment tests for sandbox writes recorded on existing snapshots."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.history_events import scheduled_activity_names
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    start_parent,
    story,
    wait_snapshot,
)

from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.models import SandboxWriteSpec, TicketInput, ticket_workflow_id
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows import TicketWorkflow

pytestmark = pytest.mark.asyncio

async def _wait_child_snapshot(handle: object, *, timeout: float = 10.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = await handle.query(TicketWorkflow.snapshot)  # type: ignore[union-attr]
        if last.stage is TicketStage.FAILED or last.stage is TicketStage.VERIFIED:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for child snapshot, last={last}")



def _authorized_config(tmp_config_root: Path) -> Path:
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    return authorized


async def _run_child(
    env: WorkflowEnvironment,
    payload: TicketInput,
    controller: StubController | None = None,
) -> tuple[object, StubController, Worker]:
    controller = controller or StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-sandbox-{payload.execution_id}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    return handle, controller, worker


async def test_child_allows_in_bounds_write_and_records_authorized_tree(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    authorized = _authorized_config(tmp_config_root)
    payload = TicketInput(
        ticket_id="T1",
        execution_id="exec-in",
        story_id="STORY-in",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        sandbox_write=SandboxWriteSpec(path="src/ok.txt", content="inside"),
    )
    handle, controller, worker = await _run_child(workflow_env, payload)
    try:
        result = await handle.result()
        assert result.stage is TicketStage.VERIFIED
        assert result.sandbox is not None
        assert result.sandbox.status == "allowed"
        worktree = Path(result.sandbox.authorized_worktree)
        assert worktree == authorized / "T1" / "exec-in"
        assert (worktree / "src" / "ok.txt").read_text(encoding="utf-8") == "inside"
        claim = controller.workspace.get("T1", "exec-in")
        assert claim is not None
        assert claim.authorized_worktree == str(worktree)
    finally:
        await worker.__aexit__(None, None, None)


async def test_child_rejects_out_of_bounds_write_and_records_it(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    authorized = _authorized_config(tmp_config_root)
    outside = tmp_config_root.parent / "external-repo" / "secret.txt"
    outside.parent.mkdir()
    payload = TicketInput(
        ticket_id="T1",
        execution_id="exec-out",
        story_id="STORY-out",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        sandbox_write=SandboxWriteSpec(path=str(outside), content="nope"),
    )
    handle, _controller, worker = await _run_child(workflow_env, payload)
    try:
        snap = await _wait_child_snapshot(handle)
        assert snap.stage is TicketStage.FAILED
        assert snap.sandbox is not None
        assert snap.sandbox.status == "rejected"
        assert "越出" in snap.sandbox.reason or "盘符" in snap.sandbox.reason
        assert not outside.exists()
        history = await handle.fetch_history()
        assert "apply_sandbox_write" in scheduled_activity_names(history)
        assert "implement_ticket" not in scheduled_activity_names(history)
        worktree = authorized / "T1" / "exec-out"
        assert worktree.is_dir()
        assert list(worktree.rglob("*")) == [] or all(item.is_dir() for item in worktree.rglob("*"))
    finally:
        await worker.__aexit__(None, None, None)


async def test_parent_snapshot_records_out_of_bounds_rejection(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    _authorized_config(tmp_config_root)
    outside = tmp_config_root.parent / "other-repo" / "x.txt"
    outside.parent.mkdir()
    payload = story(tmp_config_root, [complete_ticket("A")])
    payload.sandbox_write = SandboxWriteSpec(path=str(outside), content="nope")
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.tickets
                and item.tickets[0].sandbox is not None
                and item.tickets[0].sandbox.status == "rejected"
            ),
        )
        record = snap.tickets[0].sandbox
        assert record is not None
        assert "越出" in record.reason or "未授权" in record.reason
        assert snap.tickets[0].stage is TicketStage.FAILED
        assert snap.tickets[0].unhandled_failure is True
        assert not outside.exists()
    finally:
        await worker.__aexit__(None, None, None)


async def test_unconfigured_workspace_rejects_write_before_authorization(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    outside = tmp_config_root.parent / "external-repo" / "file.txt"
    outside.parent.mkdir()
    payload = TicketInput(
        ticket_id="T1",
        execution_id="exec-unauth",
        story_id="STORY-unauth",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        sandbox_write=SandboxWriteSpec(path=str(outside), content="nope"),
    )
    handle, controller, worker = await _run_child(workflow_env, payload)
    try:
        snap = await _wait_child_snapshot(handle)
        assert snap.stage is TicketStage.FAILED
        assert snap.sandbox is not None
        assert "未授权" in snap.sandbox.reason
        assert not outside.exists()
        claim = controller.workspace.get("T1", "exec-unauth")
        assert claim is not None
        assert claim.authorized_worktree == ""
    finally:
        await worker.__aexit__(None, None, None)
