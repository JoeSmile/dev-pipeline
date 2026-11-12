"""DP-26 acceptance: parent execution is a Story; each Ticket is a Child."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.stub_activities import StubController
from tests.transition_oracles import ticket_views_from_stages
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    stage_of,
    start_parent,
    started,
    story,
    wait_snapshot,
)

from dev_pipeline.models import TicketSpec, story_workflow_id, ticket_workflow_id
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.transitions import can_enter_retro, can_enter_test_deploying
from dev_pipeline.workflows import DevPipelineWorkflow
from dev_pipeline.workflows.parent_orchestrate import orchestration_should_resume
from dev_pipeline.workflows.parent_state import ParentState


def test_parent_execution_is_story_and_child_is_ticket() -> None:
    assert story_workflow_id("STORY-1", "exec-1") == "story-STORY-1-exec-1"
    assert ticket_workflow_id("T-9", "exec-1") == "T-9+exec-1"
    assert "required" not in TicketSpec.__dataclass_fields__


def test_required_tickets_must_all_be_verified_before_retro() -> None:
    verified = ticket_views_from_stages({"A": TicketStage.VERIFIED, "B": TicketStage.VERIFIED})
    assert can_enter_test_deploying(verified) is True
    assert can_enter_retro(verified, TestDeployStatus.SUCCEEDED) is True
    assert can_enter_retro(verified, TestDeployStatus.NOT_STARTED) is False

    failed = ticket_views_from_stages({"A": TicketStage.FAILED, "B": TicketStage.VERIFIED})
    blocked = ticket_views_from_stages({"A": TicketStage.BLOCKED, "B": TicketStage.VERIFIED})
    rework = ticket_views_from_stages(
        {"A": TicketStage.IMPLEMENTING, "B": TicketStage.VERIFIED},
        unhandled={"A": False, "B": False},
    )
    dropped = ticket_views_from_stages(
        {"A": TicketStage.DROPPED, "B": TicketStage.VERIFIED},
        unhandled={"A": False, "B": False},
    )
    for tickets in (failed, blocked, rework):
        assert can_enter_test_deploying(tickets) is False
        assert can_enter_retro(tickets, TestDeployStatus.SUCCEEDED) is False
    assert can_enter_test_deploying(dropped) is True
    assert can_enter_retro(dropped, TestDeployStatus.SUCCEEDED) is True


def test_orchestration_resumes_when_dependent_is_already_eligible() -> None:
    state = ParentState()
    state.install_tickets(
        [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B", depends_on=["A"])]
    )
    state.tickets["A"].stage = TicketStage.VERIFIED
    state.tickets["A"].started = True
    state.tickets["B"].stage = TicketStage.PENDING
    state.tickets["B"].started = False
    state.tickets["B"].depends_on = ["A"]
    state.ticket_event = False
    state.abort_requested = False
    assert orchestration_should_resume(state) is True
    state.tickets["A"].stage = TicketStage.IMPLEMENTING
    assert orchestration_should_resume(state) is False


@pytest.mark.asyncio
async def test_authorize_is_rejected_until_unhandled_failure_is_handled(
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
                stage_of(item, "A") is TicketStage.FAILED
                and stage_of(item, "B") is TicketStage.VERIFIED
            ),
        )
        assert {item.ticket_id for item in snap.tickets if item.child_created} == {"A", "B"}
        assert started(snap, "B")
        assert snap.stage is ParentStage.IMPLEMENTING
        child_b = workflow_env.client.get_workflow_handle(
            ticket_workflow_id("B", payload.execution_id)
        )
        description = await child_b.describe()
        assert description.status.name not in {"CANCELED", "CANCELLED", "TERMINATED"}
        await handle.signal(DevPipelineWorkflow.authorize_test_deploy)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None
                and item.last_rejection.signal == "authorize_test_deploy"
            ),
        )
        assert snap.stage is ParentStage.IMPLEMENTING
        assert "失败尚未处理" in (snap.last_rejection.reason if snap.last_rejection else "")
        assert snap.waiting_signal == "retry_ticket_or_drop_ticket_or_abort"
    finally:
        await handle.signal(DevPipelineWorkflow.abort, {"reason": "test-cleanup"})
        await worker.__aexit__(None, None, None)
