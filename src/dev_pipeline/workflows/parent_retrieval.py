"""Scheme-phase RAG retrieval mixed into DevPipelineWorkflow.

Must stay deterministic: corpus I/O lives in the retrieve_scheme_knowledge
Activity, and usage is booked on the existing DP-12 budget ledger.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from dev_pipeline.budget import decide_start
from dev_pipeline.retrieval import (
    RETRIEVAL_FAILED,
    RETRIEVAL_NO_HIT,
    RETRIEVAL_OVER_BUDGET,
    RETRIEVAL_SOURCE_REAL,
    RETRIEVAL_SUCCESS,
    SchemeRetrievalInput,
    SchemeRetrievalResult,
    build_scheme_query,
    make_record,
    result_from_record,
)
from dev_pipeline.workflows.parent_state import ParentState

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.retrieval import retrieve_scheme_knowledge

ACTIVITY_TIMEOUT = timedelta(seconds=60)
NO_RETRY = RetryPolicy(maximum_attempts=1)
_ACTIVITY_FAILED = "检索 Activity 失败，未形成有效命中"


class ParentRetrievalMixin:
    """Run scheme-phase retrieval through the DP-12 gate after decide_scheme."""

    _state: ParentState

    async def _apply_scheme_retrieval(self) -> None:
        """Stamp an honest retrieval record on the scheme decision.

        Inherited schemes keep their existing mark and are not searched again.
        Over-budget starts are refused by the existing ledger and never invent
        已检索到 evidence.
        """

        state = self._state
        if state.scheme is None or state.input is None or state.budget_config is None:
            return
        query = build_scheme_query(
            state.scheme.tradeoff,
            [item.statement for item in state.scheme.hypotheses],
            state.scope.in_scope if state.scope is not None else "",
        )
        await workflow.wait_condition(lambda: not state.costly_busy or state.abort_requested)
        if state.abort_requested:
            return
        state.costly_busy = True
        try:
            result = await self._run_scheme_retrieval(query)
        finally:
            state.costly_busy = False
        self._stamp_scheme_retrieval(result)

    async def _run_scheme_retrieval(self, query: str) -> SchemeRetrievalResult:
        state = self._state
        assert state.budget_config is not None
        assert state.input is not None
        decision = decide_start(
            state.budget_config,
            state.budget_spent,
            state.budget_stopped_paths,
            "rag",
        )
        if not decision.allowed:
            if decision.unit is not None and state.budget_last_breach is None:
                self._record_breach(  # type: ignore[attr-defined]
                    "rag",
                    decision.unit,
                    decision.reason,
                )
            record = make_record(
                RETRIEVAL_OVER_BUDGET,
                source=RETRIEVAL_SOURCE_REAL,
                query=query,
                reason=decision.reason,
            )
            return result_from_record(record)
        try:
            result: SchemeRetrievalResult = await workflow.execute_activity(
                retrieve_scheme_knowledge,
                SchemeRetrievalInput(
                    story_id=state.input.story_id,
                    execution_id=state.input.execution_id,
                    config_root=state.input.config_root,
                    query=query,
                ),
                start_to_close_timeout=ACTIVITY_TIMEOUT,
                retry_policy=NO_RETRY,
            )
        except ActivityError as exc:
            reason = str(exc) or _ACTIVITY_FAILED
            return result_from_record(
                make_record(
                    RETRIEVAL_FAILED,
                    source=RETRIEVAL_SOURCE_REAL,
                    query=query,
                    reason=reason,
                )
            )
        self._book_scheme_retrieval_usage(result)
        return result

    def _book_scheme_retrieval_usage(self, result: SchemeRetrievalResult) -> None:
        """Debit the DP-12 ledger and stop the rag path on 无命中."""

        if result.usage.tokens or result.usage.calls or result.usage.cost:
            self._apply_usage("rag", result.usage)  # type: ignore[attr-defined]
        if result.outcome == RETRIEVAL_NO_HIT:
            reason = (
                result.record.evidence_summary
                or "路径 rag 无新证据，已停止该路径，不再继续消耗预算。"
            )
            self._stop_path("rag", reason, no_new_evidence=True)  # type: ignore[attr-defined]

    def _stamp_scheme_retrieval(self, result: SchemeRetrievalResult) -> None:
        state = self._state
        state.scheme_retrieval = result.record
        if state.scheme is None:
            return
        state.scheme.retrieval = result.outcome
        if result.outcome == RETRIEVAL_SUCCESS and not result.record.hits:
            state.scheme.retrieval = RETRIEVAL_FAILED
            state.scheme_retrieval = make_record(
                RETRIEVAL_FAILED,
                source=result.source,
                query=result.record.query,
                reason="检索结果缺少有效命中，不能记为检索成功",
                usage=result.usage,
                corpus_root=result.record.corpus_root,
            )
