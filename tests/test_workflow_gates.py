"""Workflow tests for authorize, retry, promote, abort, and direct-start gates."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    stage_of,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
    wait_until,
)

from dev_pipeline.constants import DEFAULT_TASK_QUEUE, WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import (
    AbortRequest,
    PromoteRuleRequest,
    TicketInput,
    TicketResult,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow

pytestmark = pytest.mark.asyncio


def _ticket_stage(result: object) -> object:
    if isinstance(result, TicketResult):
        return result.stage
    if isinstance(result, dict):
        return result.get("stage")
    return getattr(result, "stage", None)


async def test_authorize_test_deploy_is_required_before_publish_or_retro(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.waiting_signal is not None
                and WAITING_AUTHORIZE_TEST_DEPLOY in item.waiting_signal
            ),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        assert snap.stage not in {ParentStage.TEST_DEPLOYING, ParentStage.RETRO, ParentStage.DONE}
        assert controller.publish_calls == 0
        await asyncio.sleep(0.3)
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.stage is ParentStage.IMPLEMENTING
        assert snap.test_deploy_status is TestDeployStatus.NOT_STARTED
        assert WAITING_AUTHORIZE_TEST_DEPLOY in (snap.waiting_signal or "")
        assert controller.publish_calls == 0
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-waiting"))
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.BLOCKED
        assert controller.publish_calls == 0
    finally:
        await worker.__aexit__(None, None, None)


async def test_drop_ticket_unblocks_test_deploying(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
    )
    controller = StubController()
    controller.implement_errors.add("B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: (
                stage_of(item, "A") is TicketStage.VERIFIED
                and stage_of(item, "B") is TicketStage.FAILED
            ),
        )
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.waiting_signal != WAITING_AUTHORIZE_TEST_DEPLOY
        await handle.signal(DevPipelineWorkflow.drop_ticket, "B")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
        final = await handle.query(DevPipelineWorkflow.snapshot)
        assert stage_of(final, "B") is TicketStage.DROPPED
    finally:
        await worker.__aexit__(None, None, None)


async def test_direct_start_child_with_unmet_deps_stays_pending(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    controller = StubController()
    task_queue = f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[TicketWorkflow],
        activities=list(controller.activities()),
    )
    await worker.__aenter__()
    try:
        handle = await workflow_env.client.start_workflow(
            TicketWorkflow.run,
            TicketInput(
                ticket_id="B",
                execution_id="exec-direct",
                story_id="STORY-direct",
                config_root=str(tmp_config_root),
                parent_workflow_id="",
                depends_on=["A"],
            ),
            id="ticket-B-direct",
            task_queue=task_queue,
        )
        await asyncio.sleep(0.3)
        snap = await handle.query(TicketWorkflow.snapshot)
        assert snap.stage is TicketStage.PENDING
        assert "B" not in controller.started
        description = await handle.describe()
        assert description.status.name == "RUNNING"
        await handle.cancel()
        await asyncio.sleep(0.2)
        assert "B" not in controller.started
        assert not controller.reviewed
    finally:
        await worker.__aexit__(None, None, None)


async def test_retry_ticket_restarts_failed_child(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.implement_fail_remaining["A"] = 1
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: stage_of(item, "A") is TicketStage.FAILED)
        await handle.signal(DevPipelineWorkflow.retry_ticket, "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert stage_of(snap, "A") is TicketStage.VERIFIED
        assert controller.started.count("A") >= 2
    finally:
        await worker.__aexit__(None, None, None)


async def test_promote_rule_records_request_and_rejects_automatic(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A"))
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            PromoteRuleRequest(rule_name="global-skill", automatic=True),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.promote_rule_status is not None and "拒绝" in (item.promote_rule_status or "")
            ),
        )
        assert snap.last_illegal is not None
        assert "拒绝" in snap.last_illegal
        await handle.signal(
            DevPipelineWorkflow.promote_rule,
            PromoteRuleRequest(rule_name="global-skill", automatic=False),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.promote_rule_status is not None
                and "已记录" in (item.promote_rule_status or "")
            ),
        )
        assert "已记录" in (snap.promote_rule_status or "")
        controller.release("implement", "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_abort_stops_running_children(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B")],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(handle, lambda item: started(item, "A") and started(item, "B"))
        await wait_until(lambda: set(controller.started) >= {"A", "B"})
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop-children"))
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.BLOCKED
        assert result.blocked_reason is not None
        assert controller.publish_calls == 0
        assert not controller.reviewed
        await asyncio.sleep(0.4)
        for ticket_id in ("A", "B"):
            child = workflow_env.client.get_workflow_handle(
                ticket_workflow_id(ticket_id, payload.execution_id)
            )
            description = await child.describe()
            if description.status.name == "COMPLETED":
                child_result = await child.result()
                stage = _ticket_stage(child_result)
                assert stage in {TicketStage.DROPPED, TicketStage.DROPPED.value}
            else:
                assert description.status.name in {
                    "CANCELED",
                    "CANCELLED",
                    "TERMINATED",
                    "FAILED",
                }
        assert not controller.reviewed
    finally:
        await worker.__aexit__(None, None, None)
