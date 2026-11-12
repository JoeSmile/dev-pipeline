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


@workflow.defn
class DevPipelineWorkflow(
    ParentSignalMixin,
    ParentChildSignalMixin,
    ParentReopenMixin,
    ParentBudgetMixin,
    ParentJiraMirrorMixin,
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
            await self._move(ParentStage.TICKETS_READY, "no_high_risk_assumption")
            state.waiting = None
        if state.abort_requested:
            return await self._block("abort")
        if self._should_start_children():
            await self._move(ParentStage.IMPLEMENTING, "start_eligible_children")
            await self._orchestrate_tickets()
            if state.abort_requested:
                return await self._block(state.abort_reason)
            return await self._run_test_deploy_and_retro()
        await workflow.wait_condition(lambda: state.abort_requested)
        return await self._block(state.abort_reason)

    def _should_start_children(self) -> bool:
        """Skeleton tests may opt into Child dispatch; production stays at TicketsReady."""
        payload = self._state.input
        return payload is not None and payload.auto_start_children

    @workflow.query
    def snapshot(self) -> PipelineSnapshot:
        """Return a thin, queryable view of parent and ticket progress."""
        return self._state.snapshot()
