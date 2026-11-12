"""DevPipelineWorkflow: one parent execution equals one Story."""

from __future__ import annotations

from temporalio import workflow

from dev_pipeline.models import PipelineResult, PipelineSnapshot, StoryInput
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows.child_start import TicketChildHandle
from dev_pipeline.workflows.parent_budget import ParentBudgetMixin
from dev_pipeline.workflows.parent_child_signals import ParentChildSignalMixin
from dev_pipeline.workflows.parent_jira_mirror import ParentJiraMirrorMixin
from dev_pipeline.workflows.parent_orchestrate import ParentOrchestrationMixin
from dev_pipeline.workflows.parent_reopen import ParentReopenMixin
from dev_pipeline.workflows.parent_signals import ParentSignalMixin
from dev_pipeline.workflows.parent_state import ParentState
from dev_pipeline.workflows.parent_tickets import ParentTicketsMixin


@workflow.defn
class DevPipelineWorkflow(
    ParentSignalMixin,
    ParentChildSignalMixin,
    ParentReopenMixin,
    ParentBudgetMixin,
    ParentJiraMirrorMixin,
    ParentTicketsMixin,
    ParentOrchestrationMixin,
):
    """Parent legal path, human gates, child orchestration, and TestDeploying."""

    def __init__(self) -> None:
        self._state = ParentState()
        self._children: dict[str, TicketChildHandle] = {}

    @workflow.run
    async def run(self, payload: StoryInput) -> PipelineResult:
        """Run one Story execution from Intake through Done or Blocked.

        Args:
            payload: Story identifiers, required config-root, and the ticket list.

        Returns:
            Terminal parent result (Done or Blocked).
        """
        state = self._state
        await self._bootstrap(payload)
        await self._wait_for_scope()
        if state.abort_requested:
            return await self._block("abort")
        await self._wait_for_scheme()
        if state.abort_requested:
            return await self._block("abort")
        if state.scheme is not None and state.scheme.has_high_risk_assumption:
            blocked = await self._enter_optional_spike()
            if blocked is not None:
                return blocked
        else:
            await self._enter_tickets_ready("no_high_risk_assumption")
        if state.abort_requested:
            return await self._block("abort")
        blocked_list = await self._wait_for_complete_ticket_list()
        if blocked_list is not None:
            return blocked_list
        if self._should_start_children():
            await self._persist_tickets_to_jira()
            await self._move(ParentStage.IMPLEMENTING, "start_eligible_children")
            await self._create_all_ticket_children()
            await self._orchestrate_tickets()
            if state.abort_requested:
                return await self._block(state.abort_reason)
            return await self._run_test_deploy_and_retro()
        await workflow.wait_condition(lambda: state.abort_requested)
        return await self._block(state.abort_reason)

    @workflow.query
    def snapshot(self) -> PipelineSnapshot:
        """Return a thin, queryable view of parent and ticket progress."""
        return self._state.snapshot()
