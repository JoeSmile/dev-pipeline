"""Terminal parking and reopen start helpers mixed into DevPipelineWorkflow."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from dev_pipeline.models import PipelineResult, ReopenStartInput, ReopenStartResult
from dev_pipeline.workflows.parent_state import ParentState

REOPEN_START_TIMEOUT = timedelta(seconds=30)
REOPEN_START_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=5),
)


class ParentReopenMixin:
    """Keep a terminal parent signalable until a legal reopen starts a new execution."""

    _state: ParentState

    async def _finish_terminal(self, result: PipelineResult) -> PipelineResult:
        """Park at the business terminal stage so reopen can still be received."""
        await self._await_reopen_then_close()
        return result

    async def _await_reopen_then_close(self) -> None:
        """Start a new parent execution after a legal reopen, then allow close."""
        state = self._state
        while not state.reopen_started:
            await workflow.wait_condition(lambda: state.reopen_request is not None)
            await self._start_reopened_execution()

    async def _start_reopened_execution(self) -> None:
        """Call the start-new-execution Activity; leave the old stage unchanged."""
        state = self._state
        request = state.reopen_request
        payload = state.input
        if request is None or payload is None:
            return
        outcome = await workflow.execute_activity(
            "start_reopened_workflow",
            ReopenStartInput(
                story_id=payload.story_id,
                execution_id=request.execution_id,
                config_root=payload.config_root,
                task_queue=workflow.info().task_queue,
                tickets=list(payload.tickets),
                summary=payload.summary,
                intake=payload.intake,
                inherited_scope=state.scope,
            ),
            start_to_close_timeout=REOPEN_START_TIMEOUT,
            retry_policy=REOPEN_START_RETRY,
            result_type=ReopenStartResult,
        )
        if outcome.started:
            state.reopened_execution_id = outcome.execution_id
            state.reopen_status = f"已启动新执行 {outcome.execution_id}"
            state.reopen_started = True
            state.reopen_request = None
            return
        state.record_rejection("reopen", outcome.error or "启动新执行失败")
        state.reopen_status = outcome.error
        state.reopen_request = None
