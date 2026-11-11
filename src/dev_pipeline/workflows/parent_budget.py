"""Parent budget gate mixed into DevPipelineWorkflow. Must stay deterministic."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from dev_pipeline.budget import (
    COSTLY_PATHS,
    BudgetAmounts,
    BudgetBreach,
    BudgetConfig,
    CostlyPathInput,
    CostlyPathOutcome,
    CostlyPathRequest,
    CostlyPathResult,
    add_usage,
    breach_reason,
    decide_start,
    exhausted_unit,
    no_new_evidence_reason,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows.parent_state import ParentState

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.budget import (
        load_budget_config,
        run_debate,
        run_model_call,
        run_rag,
    )

ACTIVITY_TIMEOUT = timedelta(seconds=60)
NO_RETRY = RetryPolicy(maximum_attempts=1)
_ACTIVITIES = {
    "debate": run_debate,
    "rag": run_rag,
    "model": run_model_call,
}
_ALREADY_CLOSED = "父执行已结束或已中止，不能再启动耗费用路径。"


class ParentBudgetMixin:
    """Load budget config, gate costly starts, and record usage on the parent."""

    _state: ParentState

    async def _install_budget(self) -> None:
        """Load budget.toml through an Activity and install it on parent state."""
        payload = self._state.input
        assert payload is not None
        config: BudgetConfig = await workflow.execute_activity(
            load_budget_config,
            payload.config_root,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        self._state.install_budget(config)

    @workflow.update
    async def start_costly_path(self, payload: CostlyPathRequest) -> CostlyPathOutcome:
        """Start one debate / rag / model stub if the budget gate allows it."""
        state = self._state
        await workflow.wait_condition(
            lambda: state.budget_config is not None or state.abort_requested
        )
        await workflow.wait_condition(lambda: not state.costly_busy or state.abort_requested)
        if state.abort_requested or state.stage in {ParentStage.BLOCKED, ParentStage.DONE}:
            return CostlyPathOutcome(
                started=False,
                path=payload.path,
                status="refused",
                reason=_ALREADY_CLOSED,
            )
        state.costly_busy = True
        try:
            return await self._run_costly_path(payload.path)
        finally:
            state.costly_busy = False

    async def _run_costly_path(self, path: str) -> CostlyPathOutcome:
        """Check the gate, run the stub Activity, then record usage or a stop."""
        state = self._state
        assert state.budget_config is not None
        assert state.input is not None
        decision = decide_start(
            state.budget_config,
            state.budget_spent,
            state.budget_stopped_paths,
            path,
        )
        if not decision.allowed:
            if decision.unit is not None and state.budget_last_breach is None:
                self._record_breach(path, decision.unit, decision.reason)
            return CostlyPathOutcome(
                started=False,
                path=path,
                status="refused",
                reason=decision.reason,
            )
        result: CostlyPathResult = await workflow.execute_activity(
            _ACTIVITIES[path],
            CostlyPathInput(
                story_id=state.input.story_id,
                execution_id=state.input.execution_id,
                path=path,
            ),
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=NO_RETRY,
        )
        return self._record_costly_result(path, result)

    def _record_costly_result(self, path: str, result: CostlyPathResult) -> CostlyPathOutcome:
        """Apply usage and stop the path on breach or when no new evidence arrives."""
        self._apply_usage(path, result.usage)
        if not result.has_new_evidence:
            reason = no_new_evidence_reason(path)
            self._stop_path(path, reason, no_new_evidence=True)
            return CostlyPathOutcome(
                started=True,
                path=path,
                status="no_new_evidence",
                reason=reason,
                usage=result.usage,
                has_new_evidence=False,
            )
        return CostlyPathOutcome(
            started=True,
            path=path,
            status="completed",
            usage=result.usage,
            has_new_evidence=True,
        )

    def _apply_usage(self, path: str, usage: BudgetAmounts) -> None:
        state = self._state
        assert state.budget_config is not None
        state.budget_spent = add_usage(state.budget_spent, usage)
        unit = exhausted_unit(state.budget_config, state.budget_spent)
        if unit is not None:
            reason = breach_reason(path, unit, state.budget_spent, state.budget_config.limits)
            self._record_breach(path, unit, reason)

    def _record_breach(self, path: str, unit: str, reason: str) -> None:
        state = self._state
        state.budget_last_breach = BudgetBreach(bad="Bad", path=path, reason=reason, unit=unit)
        state.budget_last_stop_reason = reason
        for item in COSTLY_PATHS:
            if item not in state.budget_stopped_paths:
                state.budget_stopped_paths.append(item)

    def _stop_path(self, path: str, reason: str, *, no_new_evidence: bool) -> None:
        state = self._state
        if path not in state.budget_stopped_paths:
            state.budget_stopped_paths.append(path)
        if no_new_evidence and path not in state.budget_no_new_evidence_paths:
            state.budget_no_new_evidence_paths.append(path)
        state.budget_last_stop_reason = reason
