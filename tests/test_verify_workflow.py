"""WorkflowEnvironment tests for Verifying: evidence, fail-back, and probes."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from temporalio.client import WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment
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

from dev_pipeline.activities.sandbox import apply_sandbox_write
from dev_pipeline.activities.stubs import implement_ticket
from dev_pipeline.activities.verify import verify_ticket
from dev_pipeline.activities.workspace import claim_ticket_workspace
from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.models import TicketInput, story_workflow_id, ticket_workflow_id
from dev_pipeline.review import MAX_FILE_LINES
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.verify import (
    DEFAULT_EVIDENCE_RELPATH,
    FIELD_COMMAND,
    FIELD_EVIDENCE_PATH,
    FIELD_RED_LINE,
    FIELD_RESULT,
    RESULT_HELD,
    VERIFY_MODE_DRY_RUN,
    VERIFY_MODE_HISTORY,
    parse_evidence_text,
)
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow
from dev_pipeline.workspace import reset_default_claim_store

pytestmark = pytest.mark.asyncio


async def _finish(handle: object, worker: Worker) -> None:
    try:
        desc = await handle.describe()  # type: ignore[union-attr]
        if desc.status not in {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.FAILED,
        }:
            await handle.terminate()  # type: ignore[union-attr]
    except Exception:
        pass
    await worker.__aexit__(None, None, None)


def _swap_real_verify(controller: StubController, *, real_implement: bool = False) -> list[object]:
    skip = {"verify_ticket", "claim_ticket_workspace", "apply_sandbox_write"}
    if real_implement:
        skip.add("implement_ticket")
    kept = [item for item in controller.activities() if getattr(item, "__name__", "") not in skip]
    kept.extend([claim_ticket_workspace, apply_sandbox_write, verify_ticket])
    if real_implement:
        kept.append(implement_ticket)
    return kept


async def _run_child(
    env: WorkflowEnvironment,
    payload: TicketInput,
    activities: list[object],
) -> tuple[object, Worker]:
    task_queue = f"{DEFAULT_TASK_QUEUE}-verify-{payload.execution_id}"
    worker = Worker(
        env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=activities,
    )
    await worker.__aenter__()
    handle = await env.client.start_workflow(
        TicketWorkflow.run,
        payload,
        id=ticket_workflow_id(payload.ticket_id, payload.execution_id),
        task_queue=task_queue,
    )
    return handle, worker


async def _wait_child(
    handle: object,
    stages: frozenset[TicketStage],
    *,
    timeout: float = 20.0,
):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = await handle.query(TicketWorkflow.snapshot)  # type: ignore[union-attr]
        if last.stage in stages:
            return last
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for child snapshot, last={last}")


async def test_passing_verify_writes_evidence_and_reaches_verified(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = tmp_config_root.parent / "authorized-pass"
    write_workspace_toml(tmp_config_root, authorized)
    payload = TicketInput(
        ticket_id="T1",
        execution_id="exec-pass",
        story_id="STORY-pass",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
    )
    controller = StubController()
    handle, worker = await _run_child(
        workflow_env, payload, _swap_real_verify(controller, real_implement=True)
    )
    try:
        snap = await _wait_child(handle, frozenset({TicketStage.VERIFIED}))
        assert snap.stage is TicketStage.VERIFIED
        assert snap.verify is not None
        assert snap.verify.passed is True
        assert snap.verify.ticket_id == "T1"
        assert snap.verify.execution_id == "exec-pass"
        assert any(item.result == RESULT_HELD for item in snap.verify.lines)
        evidence = Path(snap.verify.evidence_path)
        assert evidence.is_file()
        raw = evidence.read_bytes()
        text = raw.decode("utf-8")
        assert FIELD_RED_LINE in text
        assert FIELD_COMMAND in text
        assert FIELD_RESULT in text
        assert FIELD_EVIDENCE_PATH in text
        parsed = parse_evidence_text(text)
        assert parsed.passed is True
        history = await handle.fetch_history()  # type: ignore[union-attr]
        assert "verify_ticket" in scheduled_activity_names(history)
        worktree = authorized / "T1" / "exec-pass"
        assert evidence == worktree / DEFAULT_EVIDENCE_RELPATH
    finally:
        await _finish(handle, worker)
        reset_default_claim_store()


async def test_dry_run_and_history_only_do_not_reach_verified(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = tmp_config_root.parent / "authorized-probe"
    write_workspace_toml(tmp_config_root, authorized)
    controller = StubController()
    activities = _swap_real_verify(controller)
    payload = TicketInput(
        ticket_id="T-dry",
        execution_id="exec-dry",
        story_id="STORY-dry",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        verify_mode=VERIFY_MODE_DRY_RUN,
    )
    controller.hold("implement", "T-dry")
    handle, worker = await _run_child(workflow_env, payload, activities)
    try:
        snap = await _wait_child(handle, frozenset({TicketStage.IMPLEMENTING}))
        # First implement is held; release once, then re-hold for fail-back.
        controller.release("implement", "T-dry")
        await wait_until(lambda: controller.started.count("T-dry") >= 1)
        controller.hold("implement", "T-dry")
        snap = await _wait_child(handle, frozenset({TicketStage.IMPLEMENTING}))
        await wait_until(lambda: controller.started.count("T-dry") >= 2)
        snap = await handle.query(TicketWorkflow.snapshot)
        assert snap.stage is TicketStage.IMPLEMENTING
        assert snap.verify is not None
        assert snap.verify.passed is False
        assert "dry-run 或仅 History" in (snap.verify.reason or snap.detail)
        history = await handle.fetch_history()
        assert "verify_ticket" in scheduled_activity_names(history)
    finally:
        controller.release("implement", "T-dry")
        await _finish(handle, worker)
        reset_default_claim_store()

    controller = StubController()
    payload = TicketInput(
        ticket_id="T-hist",
        execution_id="exec-hist",
        story_id="STORY-hist",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        verify_mode=VERIFY_MODE_HISTORY,
    )
    controller.hold("implement", "T-hist")
    handle, worker = await _run_child(workflow_env, payload, _swap_real_verify(controller))
    try:
        controller.release("implement", "T-hist")
        await wait_until(lambda: controller.started.count("T-hist") >= 1)
        controller.hold("implement", "T-hist")
        await _wait_child(handle, frozenset({TicketStage.IMPLEMENTING}))
        await wait_until(lambda: controller.started.count("T-hist") >= 2)
        snap = await handle.query(TicketWorkflow.snapshot)
        assert snap.stage is TicketStage.IMPLEMENTING
        assert snap.verify is not None
        assert snap.verify.passed is False
    finally:
        controller.release("implement", "T-hist")
        await _finish(handle, worker)
        reset_default_claim_store()


async def test_verify_failure_returns_only_that_ticket_and_does_not_enter_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    suffix = "vf"
    authorized = tmp_config_root.parent / f"authorized-{suffix}"
    write_workspace_toml(tmp_config_root, authorized)
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
        suffix=suffix,
    )
    tree_a = authorized / "A" / payload.execution_id
    tree_a.mkdir(parents=True, exist_ok=True)
    (tree_a / "huge.py").write_text(
        "\n".join(f"line-{i}" for i in range(MAX_FILE_LINES + 1)),
        encoding="utf-8",
    )
    controller = StubController()
    controller.hold("review", "A")
    task_queue = f"{DEFAULT_TASK_QUEUE}-vf-{suffix}"
    from dev_pipeline.activities.reopen import ReopenStarter

    starter = ReopenStarter(workflow_env.client)
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*_swap_real_verify(controller), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await workflow_env.client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: stage_of(item, "A") is TicketStage.REVIEWING,
        )
        controller.hold("implement", "A")
        controller.release("review", "A")
        snap = await wait_snapshot(
            handle, lambda item: stage_of(item, "A") is TicketStage.IMPLEMENTING
        )
        await wait_until(lambda: controller.started.count("A") >= 2)
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        # Re-query after the second implement has been requested.
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        ticket = next(item for item in snap.tickets if item.ticket_id == "A")
        assert ticket.stage is TicketStage.IMPLEMENTING
        assert ticket.verify is not None
        assert ticket.verify.passed is False
        assert ticket.verify.evidence_path
        assert Path(ticket.verify.evidence_path).is_file()
        assert stage_of(snap, "B") in {TicketStage.VERIFYING, TicketStage.VERIFIED}
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.stage is not ParentStage.RETRO
        child = workflow_env.client.get_workflow_handle(
            ticket_workflow_id("A", payload.execution_id)
        )
        history = await child.fetch_history()
        assert "verify_ticket" in scheduled_activity_names(history)
    finally:
        controller.release("implement", "A")
        controller.release("review", "A")
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


async def test_out_of_bounds_verify_write_does_not_pass(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    reset_default_claim_store()
    authorized = tmp_config_root.parent / "authorized-oob"
    write_workspace_toml(tmp_config_root, authorized)
    outside = tmp_config_root.parent / "external-evidence" / "verify.md"
    outside.parent.mkdir()
    controller = StubController()
    payload = TicketInput(
        ticket_id="T-oob",
        execution_id="exec-oob",
        story_id="STORY-oob",
        config_root=str(tmp_config_root),
        parent_workflow_id="",
        verify_evidence_path=str(outside),
    )
    controller.hold("implement", "T-oob")
    handle, worker = await _run_child(workflow_env, payload, _swap_real_verify(controller))
    try:
        controller.release("implement", "T-oob")
        await wait_until(lambda: controller.started.count("T-oob") >= 1)
        controller.hold("implement", "T-oob")
        await _wait_child(handle, frozenset({TicketStage.IMPLEMENTING}))
        await wait_until(lambda: controller.started.count("T-oob") >= 2)
        snap = await handle.query(TicketWorkflow.snapshot)
        assert snap.stage is TicketStage.IMPLEMENTING
        assert snap.verify is not None
        assert snap.verify.passed is False
        assert snap.verify.write_rejected is True
        assert not outside.exists()
    finally:
        controller.release("implement", "T-oob")
        await _finish(handle, worker)
        reset_default_claim_store()
