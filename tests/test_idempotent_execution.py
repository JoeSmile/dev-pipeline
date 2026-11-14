"""Workflow tests for DP-11 idempotent ticket execution."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from temporalio.api.enums.v1 import WorkflowIdReusePolicy
from temporalio.common import WorkflowIDConflictPolicy
from temporalio.common import WorkflowIDReusePolicy as SdkReuse
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.history_events import (
    child_id_reuse_policies,
    initiated_child_workflow_ids,
    started_child_workflow_ids,
)
from tests.idempotent_probes import CompletedChildReuseWorkflow, ReentryChildStartWorkflow
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    scheme_decision,
    stage_of,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.constants import DEFAULT_TASK_QUEUE
from dev_pipeline.models import (
    AbortRequest,
    ReopenRequest,
    ScopeLock,
    TicketInput,
    TicketResult,
    story_workflow_id,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow
from dev_pipeline.workspace import MAINLINE_BRANCH

pytestmark = pytest.mark.asyncio


def _stage_matches(got: object, expected: TicketStage) -> bool:
    """Compare ticket stages by value, not identity.

    Workflow Query results can deserialize to an equivalent enum instance
    that is not the same object as the test process member, so ``is``
    can fail even when the snapshot already shows the expected stage.
    """

    return got == expected or getattr(got, "value", got) == expected.value



REJECT_DUPLICATE = int(WorkflowIdReusePolicy.WORKFLOW_ID_REUSE_POLICY_REJECT_DUPLICATE)


def _ticket_input(config_root: Path, ticket_id: str, execution_id: str) -> TicketInput:
    return TicketInput(
        ticket_id=ticket_id,
        execution_id=execution_id,
        story_id=f"STORY-{execution_id}",
        config_root=str(config_root),
        parent_workflow_id="",
        depends_on=[],
    )


async def wait_claim(controller: StubController, ticket_id: str, execution_id: str):
    """Wait until the workspace claim for this key has been recorded."""
    await wait_until(lambda: controller.workspace.get(ticket_id, execution_id) is not None)
    claim = controller.workspace.get(ticket_id, execution_id)
    assert claim is not None
    return claim


async def test_same_key_in_progress_has_one_claim_and_one_child_id(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        await wait_claim(controller, "A", payload.execution_id)
        child_id = ticket_workflow_id("A", payload.execution_id)
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == [child_id]
        assert started_child_workflow_ids(history) == [child_id]
        assert child_id_reuse_policies(history) == [REJECT_DUPLICATE]
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        claim = controller.workspace.get("A", payload.execution_id)
        assert claim is not None
        assert claim.workspace_id == f"ws/A/{payload.execution_id}"

        description = await handle.describe()
        with pytest.raises(WorkflowAlreadyStartedError):
            await workflow_env.client.start_workflow(
                TicketWorkflow.run,
                _ticket_input(tmp_config_root, "A", payload.execution_id),
                id=child_id,
                task_queue=description.task_queue,
                id_reuse_policy=SdkReuse.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
            )
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        history = await handle.fetch_history()
        assert started_child_workflow_ids(history) == [child_id]
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-reentry"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_reentry_probe_starts_the_child_only_once(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    controller.hold("implement", "A")
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    payload = _ticket_input(tmp_config_root, "A", execution_id)
    task_queue = f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[ReentryChildStartWorkflow, TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    try:
        handle = await workflow_env.client.start_workflow(
            ReentryChildStartWorkflow.run,
            payload,
            id=f"probe-reentry-{execution_id}",
            task_queue=task_queue,
        )
        outcome = await handle.result()
        child_id = ticket_workflow_id("A", execution_id)
        assert outcome["first_id"] == child_id
        assert outcome["second_id"] == child_id
        assert outcome["started_first"] is True
        assert outcome["started_second"] is False
        assert controller.workspace.created_count("A", execution_id) == 1
        history = await handle.fetch_history()
        assert started_child_workflow_ids(history) == [child_id]
        assert child_id_reuse_policies(history).count(REJECT_DUPLICATE) >= 1
        child = workflow_env.client.get_workflow_handle(child_id)
        await child.cancel()
    finally:
        await worker.__aexit__(None, None, None)


async def test_same_key_completed_returns_existing_result_and_one_claim(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    payload = _ticket_input(tmp_config_root, "A", execution_id)
    task_queue = f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[CompletedChildReuseWorkflow, TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    try:
        handle = await workflow_env.client.start_workflow(
            CompletedChildReuseWorkflow.run,
            payload,
            id=f"probe-completed-{execution_id}",
            task_queue=task_queue,
        )
        outcome = await handle.result()
        child_id = ticket_workflow_id("A", execution_id)
        assert outcome["first_id"] == child_id
        assert outcome["second_id"] == child_id
        assert outcome["started_first"] is True
        assert outcome["started_second"] is False
        assert outcome["stage"] == TicketStage.VERIFIED.value
        existing = await workflow_env.client.get_workflow_handle(
            child_id, result_type=TicketResult
        ).result()
        assert existing.stage is TicketStage.VERIFIED
        assert existing.ticket_id == "A"
        assert existing.execution_id == execution_id
        assert controller.workspace.created_count("A", execution_id) == 1

        with pytest.raises(WorkflowAlreadyStartedError):
            await workflow_env.client.start_workflow(
                TicketWorkflow.run,
                payload,
                id=child_id,
                task_queue=task_queue,
                id_reuse_policy=SdkReuse.REJECT_DUPLICATE,
            )
        assert controller.workspace.created_count("A", execution_id) == 1
        second = await workflow_env.client.get_workflow_handle(
            child_id, result_type=TicketResult
        ).result()
        assert second == existing
        history = await handle.fetch_history()
        assert started_child_workflow_ids(history) == [child_id]
    finally:
        await worker.__aexit__(None, None, None)


async def _wait_not_running(client: object, workflow_id: str, *, timeout: float = 15.0) -> None:
    """Wait until a workflow has left RUNNING (e.g. cancel of a held activity)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    last = None
    while loop.time() < deadline:
        last = await client.get_workflow_handle(workflow_id).describe()  # type: ignore[attr-defined]
        if last.status.name not in {"RUNNING"}:
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"timeout waiting for {workflow_id} to leave RUNNING, last={last}")


async def test_reopen_uses_new_key_and_leaves_old_claim_untouched(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        old_claim = await wait_claim(controller, "A", payload.execution_id)
        old_child_id = ticket_workflow_id("A", payload.execution_id)
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="reopen-after-claim"))
        await wait_terminal(handle)
        new_id = f"{payload.execution_id}-reopen"
        await handle.signal(DevPipelineWorkflow.reopen, ReopenRequest(execution_id=new_id))
        snap = await wait_snapshot(handle, lambda item: item.reopened_execution_id == new_id)
        assert snap.stage is ParentStage.BLOCKED
        assert snap.execution_id == payload.execution_id
        assert controller.workspace.get("A", payload.execution_id) == old_claim
        assert controller.workspace.created_count("A", payload.execution_id) == 1

        await _wait_not_running(workflow_env.client, old_child_id)

        new_handle = workflow_env.client.get_workflow_handle(
            story_workflow_id(payload.story_id, new_id)
        )
        await new_handle.signal(
            DevPipelineWorkflow.lock_scope,
            ScopeLock(in_scope="in", out_of_scope="out", locked_by="tester"),
        )
        await wait_snapshot(new_handle, lambda item: item.stage is not ParentStage.INTAKE)
        await new_handle.signal(
            DevPipelineWorkflow.decide_scheme,
            scheme_decision(payload),
        )
        await wait_snapshot(
            new_handle,
            lambda item: started(item, "A") or item.stage is ParentStage.IMPLEMENTING,
        )
        snap = await wait_snapshot(new_handle, lambda item: started(item, "A"))
        assert snap.execution_id == new_id
        new_claim = await wait_claim(controller, "A", new_id)
        new_child_id = ticket_workflow_id("A", new_id)
        assert new_child_id != old_child_id
        new_history = await new_handle.fetch_history()
        assert old_child_id not in initiated_child_workflow_ids(new_history)
        assert new_child_id in started_child_workflow_ids(new_history)
        assert new_claim.workspace_id != old_claim.workspace_id
        assert new_claim.branch_name != old_claim.branch_name
        assert controller.workspace.get("A", payload.execution_id) == old_claim
        # Old child is still not running and was not resumed by the new parent.
        await _wait_not_running(workflow_env.client, old_child_id)
        await new_handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-reopened"))
        await wait_terminal(new_handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_ticket_failure_does_not_damage_sibling_claim_or_mainline(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
    )
    controller = StubController()
    controller.implement_errors.add("A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                _stage_matches(stage_of(item, "A"), TicketStage.FAILED)
                and _stage_matches(stage_of(item, "B"), TicketStage.VERIFIED)
            ),
        )
        claim_a = controller.workspace.get("A", payload.execution_id)
        claim_b = controller.workspace.get("B", payload.execution_id)
        assert claim_a is not None
        assert claim_b is not None
        assert claim_a.workspace_id != claim_b.workspace_id
        assert claim_a.branch_name != claim_b.branch_name
        assert claim_b.branch_name != MAINLINE_BRANCH
        assert claim_a.branch_name != MAINLINE_BRANCH
        assert payload.execution_id in claim_b.workspace_id
        assert controller.workspace.created_count("B", payload.execution_id) == 1
        assert controller.workspace.get("B", payload.execution_id) == claim_b
        assert snap.stage is ParentStage.IMPLEMENTING
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-isolation"))
        await wait_terminal(handle)
        assert controller.workspace.get("B", payload.execution_id) == claim_b
    finally:
        await worker.__aexit__(None, None, None)


async def test_retry_ticket_does_not_allocate_a_second_workspace_identity(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.implement_fail_remaining["A"] = 1
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: _stage_matches(stage_of(item, "A"), TicketStage.FAILED),
        )
        first = controller.workspace.get("A", payload.execution_id)
        assert first is not None
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        await handle.signal(DevPipelineWorkflow.retry_ticket, "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
        assert controller.workspace.get("A", payload.execution_id) == first
        assert controller.workspace.created_count("A", payload.execution_id) == 1
        history = await handle.fetch_history()
        child_id = ticket_workflow_id("A", payload.execution_id)
        assert started_child_workflow_ids(history) == [child_id]
        assert controller.started.count("A") >= 2
    finally:
        await worker.__aexit__(None, None, None)
