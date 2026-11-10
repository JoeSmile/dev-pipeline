"""DevPipelineWorkflow: one parent execution equals one Story."""

from __future__ import annotations

from typing import Any

from temporalio import workflow
from temporalio.workflow import ChildWorkflowHandle

from dev_pipeline.models import PipelineResult, PipelineSnapshot, StoryInput, TicketResult
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows.parent_orchestrate import ParentOrchestrationMixin
from dev_pipeline.workflows.parent_signals import ParentSignalMixin
from dev_pipeline.workflows.parent_state import ParentState


@workflow.defn
class DevPipelineWorkflow(ParentSignalMixin, ParentOrchestrationMixin):
    """Parent legal path, human gates, child orchestration, and TestDeploying."""

    def __init__(self) -> None:
        self._state = ParentState()
        self._children: dict[str, ChildWorkflowHandle[Any, TicketResult]] = {}

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
            blocked = await self._run_spike()
            if blocked is not None:
                return blocked
        self._advance_to_implementing()
        if state.abort_requested:
            return await self._block("abort")
        await self._orchestrate_tickets()
        if state.abort_requested:
            return await self._block(state.abort_reason)
        return await self._run_test_deploy_and_retro()

    def _advance_to_implementing(self) -> None:
        self._move(ParentStage.TICKETS_READY, "no_high_risk_assumption")
        self._state.waiting = None
        if self._state.abort_requested:
            return
        self._move(ParentStage.IMPLEMENTING, "start_eligible_children")

    @workflow.query
    def snapshot(self) -> PipelineSnapshot:
        """Return a thin, queryable view of parent and ticket progress."""
        return self._state.snapshot()
