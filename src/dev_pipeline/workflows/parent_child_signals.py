"""Child-routed signals mixed into DevPipelineWorkflow."""

from __future__ import annotations

from typing import Any, cast

from temporalio import workflow
from temporalio.workflow import ChildWorkflowHandle

from dev_pipeline.models import TicketResult, TicketStateUpdate, ticket_workflow_id
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows.parent_state import ParentState


class ParentChildSignalMixin:
    """Signals that update ticket snapshots or are forwarded to a child."""

    _state: ParentState
    _children: dict[str, ChildWorkflowHandle[Any, TicketResult]]

    @workflow.signal
    async def retry_ticket(self, ticket_id: str) -> None:
        """Clear an unhandled failure and ask the child to retry from Implementing."""
        snap = self._state.tickets.get(ticket_id)
        if snap is not None:
            snap.unhandled_failure = False
            self._state.ticket_event = True
        await self._route_to_child(ticket_id, "retry_ticket")

    @workflow.signal
    async def drop_ticket(self, ticket_id: str) -> None:
        """Remove a ticket from the list so it no longer blocks TestDeploying or Retro."""
        snap = self._state.tickets.get(ticket_id)
        if snap is not None:
            snap.unhandled_failure = False
            snap.stage = TicketStage.DROPPED
            self._state.ticket_event = True
        await self._route_to_child(ticket_id, "drop_ticket")

    @workflow.signal
    def ticket_state_changed(self, update: TicketStateUpdate) -> None:
        """Accept a progress update from a child TicketWorkflow."""
        snap = self._state.tickets.get(update.ticket_id)
        if snap is None:
            return
        snap.stage = update.stage
        snap.detail = update.detail
        snap.started = True
        if update.stage is TicketStage.DROPPED:
            snap.unhandled_failure = False
        else:
            snap.unhandled_failure = update.unhandled_failure
        self._state.ticket_event = True

    async def _route_to_child(
        self, ticket_id: str, signal_name: str, payload: object | None = None
    ) -> None:
        handle = self._children.get(ticket_id)
        if handle is None:
            if self._state.input is None:
                return
            snap = self._state.tickets.get(ticket_id)
            if snap is None or not snap.started:
                return
            child_id = ticket_workflow_id(ticket_id, self._state.input.execution_id)
            handle = cast(
                ChildWorkflowHandle[Any, TicketResult],
                workflow.get_external_workflow_handle(child_id),
            )
        if payload is None:
            await handle.signal(signal_name)
        else:
            await handle.signal(signal_name, payload)
