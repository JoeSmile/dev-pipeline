"""WorkflowEnvironment tests for DP-19 TicketsReady list and child dispatch."""

from __future__ import annotations

from pathlib import Path

from temporalio.testing import WorkflowEnvironment
from tests.history_events import initiated_child_workflow_ids
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    authorize_test_deploy,
    complete_ticket,
    scheme_decision,
    scope_lock,
    start_parent,
    started,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.models import (
    PENDING_FIELD,
    AbortRequest,
    StoryInput,
    TicketListSubmission,
    TicketSpec,
    ticket_workflow_id,
)
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.workflows import DevPipelineWorkflow


def _submission(*tickets: TicketSpec, submitted_by: str = "tester") -> TicketListSubmission:
    return TicketListSubmission(tickets=list(tickets), submitted_by=submitted_by)


async def test_complete_list_enters_implementing_and_creates_one_child_per_ticket(
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
        snap = await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.IMPLEMENTING
                and all(ticket.child_created for ticket in item.tickets)
                and started(item, "A")
                and started(item, "B")
            ),
        )
        assert snap.ticket_list_complete is True
        assert {ticket.ticket_id for ticket in snap.tickets} == {"A", "B"}
        for ticket in snap.tickets:
            assert ticket.goal
            assert ticket.acceptance
            assert ticket.io_contract
            assert ticket.child_created
            assert ticket.jira_key
        assert {started(snap, "A"), started(snap, "B")} == {True}
        assert len(controller.jira_ticket_calls) == 1
        assert set(controller.jira.issues()) == {ticket.jira_key for ticket in snap.tickets}
        history = await handle.fetch_history()
        child_ids = set(initiated_child_workflow_ids(history))
        assert child_ids == {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        assert not (tmp_config_root / "TASKS.md").exists()
        controller.release("implement", "A", "B")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_incomplete_list_does_not_enter_implementing_or_dispatch_children(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [TicketSpec(ticket_id="A")], auto_start_children=True)
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.TICKETS_READY
                and item.waiting_signal == "submit_tickets"
            ),
        )
        assert snap.stage is ParentStage.TICKETS_READY
        assert snap.ticket_list_complete is False
        assert snap.waiting_signal == "submit_tickets"
        assert all(not ticket.child_created and not ticket.started for ticket in snap.tickets)
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        assert not controller.started
        assert controller.jira_ticket_calls == []
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.BLOCKED
    finally:
        await worker.__aexit__(None, None, None)


async def test_auto_start_children_true_with_empty_list_does_not_dispatch(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = StoryInput(
        story_id="STORY-empty",
        execution_id="exec-empty",
        config_root=str(tmp_config_root),
        tickets=[],
        summary="empty list",
        auto_start_children=True,
    )
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.TICKETS_READY
                and item.waiting_signal == "submit_tickets"
            ),
        )
        assert snap.stage is ParentStage.TICKETS_READY
        assert snap.ticket_list_complete is False
        assert snap.waiting_signal == "submit_tickets"
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_submit_tickets_completes_list_and_starts_children(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [], auto_start_children=False)
    controller = StubController()
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        snap = await wait_snapshot(handle, lambda item: item.waiting_signal == "submit_tickets")
        assert snap.stage is ParentStage.TICKETS_READY
        await handle.signal(
            DevPipelineWorkflow.submit_tickets,
            _submission(complete_ticket("A")),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: item.stage is ParentStage.IMPLEMENTING and started(item, "A"),
        )
        assert snap.ticket_list_complete is True
        assert snap.ticket_list_submitted_by == "tester"
        assert snap.ticket_list_submitted_at
        assert snap.tickets[0].goal == "实现 A"
        assert snap.tickets[0].jira_key
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == [
            ticket_workflow_id("A", payload.execution_id)
        ]
        controller.release("implement", "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_incomplete_submit_tickets_is_rejected_and_stage_stays(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [], auto_start_children=True)
    controller = StubController()
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await handle.signal(DevPipelineWorkflow.lock_scope, scope_lock())
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
        await handle.signal(DevPipelineWorkflow.decide_scheme, scheme_decision(payload))
        await wait_snapshot(handle, lambda item: item.waiting_signal == "submit_tickets")
        await handle.signal(
            DevPipelineWorkflow.submit_tickets,
            TicketListSubmission(
                tickets=[
                    TicketSpec(
                        ticket_id="A",
                        goal=PENDING_FIELD,
                        acceptance="验收",
                        io_contract="I/O",
                    )
                ],
                submitted_by="tester",
            ),
        )
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.last_rejection is not None and item.last_rejection.signal == "submit_tickets"
            ),
        )
        assert snap.stage is ParentStage.TICKETS_READY
        assert snap.ticket_list_complete is False
        history = await handle.fetch_history()
        assert initiated_child_workflow_ids(history) == []
        await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="stop"))
        await wait_terminal(handle)
    finally:
        await worker.__aexit__(None, None, None)


async def test_unmet_deps_child_is_created_but_stays_pending(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(
        tmp_config_root,
        [complete_ticket("A"), complete_ticket("B", depends_on=["A"])],
    )
    controller = StubController()
    controller.hold("implement", "A", "B")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(handle, lambda item: started(item, "A"))
        assert snap.stage is ParentStage.IMPLEMENTING
        by_id = {item.ticket_id: item for item in snap.tickets}
        assert by_id["A"].child_created and by_id["A"].started
        assert by_id["B"].child_created
        assert not by_id["B"].started
        assert by_id["B"].stage is TicketStage.PENDING
        assert "B" not in controller.started
        history = await handle.fetch_history()
        assert set(initiated_child_workflow_ids(history)) == {
            ticket_workflow_id("A", payload.execution_id),
            ticket_workflow_id("B", payload.execution_id),
        }
        controller.release("implement", "A")
        snap = await wait_snapshot(handle, lambda item: started(item, "B"))
        assert started(snap, "B")
        controller.release("implement", "B")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)


async def test_jira_ticket_write_failure_is_visible_and_does_not_swallow(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    payload = story(tmp_config_root, [complete_ticket("A")])
    controller = StubController()
    controller.jira_ticket_always_fail = True
    controller.hold("implement", "A")
    worker, handle = await start_parent(workflow_env, payload, controller)
    try:
        await advance_to_implementing(handle, payload)
        snap = await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.IMPLEMENTING
                and item.jira_tickets is not None
                and item.jira_tickets.lagged
                and started(item, "A")
            ),
        )
        assert snap.jira_tickets is not None
        assert snap.jira_tickets.lagged is True
        assert snap.jira_tickets.lag_marker == "Jira 写票滞后"
        assert snap.jira_tickets.lag_reason
        assert snap.stage is ParentStage.IMPLEMENTING
        assert started(snap, "A")
        controller.release("implement", "A")
        await authorize_test_deploy(handle)
        result = await wait_terminal(handle)
        assert result.stage is ParentStage.DONE
    finally:
        await worker.__aexit__(None, None, None)
