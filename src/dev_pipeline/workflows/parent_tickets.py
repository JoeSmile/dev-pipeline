"""TicketsReady completeness gate and Jira ticket persistence for the parent."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from dev_pipeline.models import PipelineResult
from dev_pipeline.stages import ParentStage
from dev_pipeline.ticket_list import JiraTicketPersistInput, is_complete_ticket_list
from dev_pipeline.workflows.parent_jira_mirror import (
    MIRROR_RETRY,
    MIRROR_TIMEOUT,
    lag_reason_from_error,
)
from dev_pipeline.workflows.parent_state import ParentState

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.jira_tickets import persist_jira_tickets

TICKET_WRITE_TIMEOUT = MIRROR_TIMEOUT
TICKET_WRITE_RETRY = RetryPolicy(
    maximum_attempts=MIRROR_RETRY.maximum_attempts,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
)
_LAG_FALLBACK = "Jira 写票失败"


class ParentTicketsMixin:
    """Wait for a complete ticket list and persist it to Jira project DP."""

    _state: ParentState

    async def _enter_tickets_ready(self, trigger: str) -> None:
        """Enter TicketsReady and advertise submit_tickets when the list is incomplete."""
        state = self._state
        if is_complete_ticket_list(state.specs):
            state.waiting = None
        else:
            state.waiting = "submit_tickets"
        move = self._move  # type: ignore[attr-defined]
        await move(ParentStage.TICKETS_READY, trigger)

    async def _wait_for_complete_ticket_list(self) -> PipelineResult | None:
        """Stay in TicketsReady until the list is complete, or abort."""
        state = self._state
        if is_complete_ticket_list(state.specs):
            state.waiting = None
            return None
        state.waiting = "submit_tickets"
        await workflow.wait_condition(
            lambda: is_complete_ticket_list(state.specs) or state.abort_requested
        )
        if state.abort_requested:
            block = self._block  # type: ignore[attr-defined]
            result: PipelineResult = await block(state.abort_reason)
            return result
        state.waiting = None
        return None

    def _should_start_children(self) -> bool:
        """Start children only when the required ticket list is complete.

        ``StoryInput.auto_start_children`` is no longer a sufficient gate.
        An empty or incomplete list must not enter Implementing or dispatch
        a Coding Child, even when that flag is True.
        """
        return is_complete_ticket_list(self._state.specs)

    async def _persist_tickets_to_jira(self) -> None:
        """Write complete tickets to Jira DP, or record a visible lag."""
        state = self._state
        payload = state.input
        if payload is None or not state.specs:
            return
        try:
            result = await workflow.execute_activity(
                persist_jira_tickets,
                JiraTicketPersistInput(
                    story_id=payload.story_id,
                    execution_id=payload.execution_id,
                    config_root=payload.config_root,
                    tickets=list(state.specs),
                ),
                start_to_close_timeout=TICKET_WRITE_TIMEOUT,
                retry_policy=TICKET_WRITE_RETRY,
            )
        except ActivityError as exc:
            state.record_jira_ticket_lag(lag_reason_from_error(exc, fallback=_LAG_FALLBACK))
            return
        state.record_jira_ticket_success(result.tickets)
