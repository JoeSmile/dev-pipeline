"""WorkflowEnvironment tests for ticket deliverables and the Reviewing gate."""

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
    story,
    wait_snapshot,
)

from dev_pipeline.activities.sandbox import apply_sandbox_write
from dev_pipeline.activities.stubs import implement_ticket
from dev_pipeline.activities.workspace import claim_ticket_workspace
from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.models import ReviewFindings, TicketInput, ticket_workflow_id
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows import TicketWorkflow
from dev_pipeline.workspace import derive_branch_name, reset_default_claim_store

pytestmark = pytest.mark.asyncio


async def _wait_child(
    handle: object,
    *,
    timeout: float = 15.0,
    stages: frozenset[TicketStage] | None = None,
):
    wanted = stages or frozenset(
        {
            TicketStage.REVIEWING,
            TicketStage.HUMAN_GATE,
            TicketStage.FAILED,
            TicketStage.VERIFIED,
            TicketStage.DROPPED,
        }
    )
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = await handle.query(TicketWorkflow.snapshot)  # type: ignore[union-attr]
        if last.stage in wanted:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for child snapshot, last={last}")


def _authorized_config(tmp_config_root: Path) -> Path:
    authorized = tmp_config_root.parent / "authorized-root"
    write_workspace_toml(tmp_config_root, authorized)
    return authorized


def _real_implement_activities(controller: StubController) -> list[object]:
    kept: list[object] = []
    for item in controller.activities():
        name = getattr(item, "__name__", "")
        if name in {"implement_ticket", "claim_ticket_workspace", "apply_sandbox_write"}:
            continue
        kept.append(item)
    kept.extend([claim_ticket_workspace, apply_sandbox_write, implement_ticket])
    return kept


async def _run_real_child(
    env: WorkflowEnvironment,
    payload: TicketInput,
    controller: StubController,
) -> tuple[object, Worker]:
    task_queue = f"{DEFAULT_TASK_QUEUE}-deliv-{payload.execution_id}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=_real_implement_activities(controller),
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    return handle, worker


async def test_complete_deliverables_enter_reviewing_and_keep_human_gate(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    try:
        _authorized_config(tmp_config_root)
        controller = StubController()
        controller.hold("review", "T1")
        controller.review_findings["T1"] = ReviewFindings(blocking=["must gate"])
        payload = TicketInput(
            ticket_id="T1",
            execution_id="exec-complete",
            story_id="STORY-deliv",
            config_root=str(tmp_config_root),
            parent_workflow_id="",
        )
        handle, worker = await _run_real_child(workflow_env, payload, controller)
        try:
            snap = await _wait_child(handle)
            assert snap.stage is TicketStage.REVIEWING
            assert snap.deliverables is not None
            assert snap.deliverables.complete is True
            assert snap.deliverables.branch_name == derive_branch_name(
                "T1", "exec-complete"
            )
            assert snap.deliverables.diff
            assert snap.deliverables.test_evidence
            assert snap.deliverables.rollback_notes
            assert snap.deliverables.tests_ran_in_worktree is True
            assert snap.deliverables.remote_pr_opened is False
            assert snap.deliverables.pushed is False
            controller.release("review", "T1")
            gated = await _wait_child(handle, stages=frozenset({TicketStage.HUMAN_GATE}))
            assert gated.stage is TicketStage.HUMAN_GATE
            # Do not send review_override: HumanGate must not be auto-passed.
            await asyncio.sleep(0.2)
            still = await handle.query(TicketWorkflow.snapshot)
            assert still.stage is TicketStage.HUMAN_GATE
        finally:
            await handle.terminate()
            await worker.__aexit__(None, None, None)
    finally:
        reset_default_claim_store()


async def test_incomplete_deliverables_do_not_enter_reviewing(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.implement_incomplete.add("T-miss")
    payload = TicketInput(
        ticket_id="T-miss",
        execution_id="exec-miss",
        story_id="STORY-miss",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    task_queue = f"{DEFAULT_TASK_QUEUE}-miss-{payload.execution_id}"
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    handle = await workflow_env.client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        snap = await _wait_child(handle)
        assert snap.stage is TicketStage.FAILED
        assert snap.deliverables is not None
        assert snap.deliverables.complete is False
        assert "交付物不齐" in snap.detail
        history = await handle.fetch_history()
        names = scheduled_activity_names(history)
        assert "implement_ticket" in names
        assert "review_ticket" not in names
    finally:
        await handle.terminate()
        await worker.__aexit__(None, None, None)


async def test_parent_snapshot_records_deliverables_without_remote_pr(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    try:
        _authorized_config(tmp_config_root)
        controller = StubController()
        controller.hold("review", "T1")
        payload = story(tmp_config_root, [complete_ticket("T1")])
        # Use the real implement activity on the parent worker.
        task_queue = f"{DEFAULT_TASK_QUEUE}-parent-deliv-{payload.execution_id}"
        from dev_pipeline.activities.reopen import ReopenStarter
        from dev_pipeline.workflows import DevPipelineWorkflow

        worker = Worker(
            workflow_env.client,
            task_queue=task_queue,
            workflows=[DevPipelineWorkflow, TicketWorkflow],
            activities=[
                *_real_implement_activities(controller),
                ReopenStarter(workflow_env.client).start_reopened_workflow,
            ],
        )
        await worker.__aenter__()
        handle = await workflow_env.client.start_workflow(
            DevPipelineWorkflow.run,
            payload,
            id=f"story-{payload.story_id}-{payload.execution_id}",
            task_queue=task_queue,
        )
        try:
            await advance_to_implementing(handle, payload)
            snap = await wait_snapshot(
                handle,
                lambda item: any(
                    ticket.stage is TicketStage.REVIEWING for ticket in item.tickets
                ),
            )
            ticket = snap.tickets[0]
            assert ticket.deliverables is not None
            assert ticket.deliverables.complete is True
            assert ticket.deliverables.remote_pr_opened is False
            assert ticket.deliverables.pushed is False
            assert ticket.stage is TicketStage.REVIEWING
        finally:
            await handle.terminate()
            await worker.__aexit__(None, None, None)
    finally:
        reset_default_claim_store()
