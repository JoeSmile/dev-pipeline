"""Human signal handlers mixed into DevPipelineWorkflow."""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from dev_pipeline.models import (
    RETRIEVAL_STUB,
    AbortRequest,
    ApproveSpikeRequest,
    PromoteRuleRequest,
    ReopenRequest,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    format_scope_lock_time,
    has_red_line_hypothesis,
)
from dev_pipeline.signal_schemas import parse_human_signal
from dev_pipeline.stages import ParentStage, TicketStage
from dev_pipeline.transitions import can_enter_test_deploying
from dev_pipeline.workflows.parent_state import ParentState

_REOPEN_SAME_ID = "reopen 必须使用新的 execution_id 启动新的父执行，不能在已损坏的本次执行上恢复。"
_REOPEN_NOT_TERMINAL = "当前执行尚未结束，不能 reopen。请先 abort。"
_REOPEN_IN_FLIGHT = "正在处理 reopen，请等待新执行启动完成。"
_PROMOTE_AUTO_REJECTED = "promote_rule 自动晋升已拒绝；全局规则不得静默升级。"
_PROMOTE_RECORDED = "promote_rule 已记录，本骨架不执行晋升（拒绝自动生效）。"


class ParentSignalMixin:
    """Human signal handlers for the parent workflow. Mixed into DevPipelineWorkflow."""

    _state: ParentState

    @workflow.signal
    def lock_scope(self, payload: Any) -> None:
        """Lock Story scope. Rejected unless the parent is still in Intake."""
        parsed = self._parsed("lock_scope", payload)
        if parsed is None:
            return
        assert isinstance(parsed, ScopeLock)
        if self._state.scope is not None or self._state.stage is not ParentStage.INTAKE:
            self._reject(
                "lock_scope",
                f"非法转移: {self._state.stage.value} --lock_scope--> ScopeLocked。"
                "当前阶段不能锁范围",
            )
            return
        self._state.scope = ScopeLock(
            in_scope=parsed.in_scope,
            out_of_scope=parsed.out_of_scope,
            locked_by=parsed.locked_by,
            locked_at=format_scope_lock_time(workflow.now()),
        )

    @workflow.signal
    def decide_scheme(self, payload: Any) -> None:
        """Record the scheme decision after scope has been locked."""
        parsed = self._parsed("decide_scheme", payload)
        if parsed is None:
            return
        assert isinstance(parsed, SchemeDecision)
        if self._state.scheme is not None or self._state.stage is not ParentStage.SCOPE_LOCKED:
            self._reject(
                "decide_scheme",
                f"非法转移: {self._state.stage.value} --decide_scheme--> SchemeDecided。"
                "当前阶段不能拍方案",
            )
            return
        self._state.scheme = SchemeDecision(
            tradeoff=parsed.tradeoff,
            hypotheses=list(parsed.hypotheses),
            decided_by=parsed.decided_by,
            decided_at=format_scope_lock_time(workflow.now()),
            retrieval=RETRIEVAL_STUB,
            tickets=list(parsed.tickets),
            has_high_risk_assumption=has_red_line_hypothesis(list(parsed.hypotheses)),
            notes=parsed.notes,
        )
        if parsed.tickets:
            self._state.install_tickets(parsed.tickets)

    @workflow.signal
    def approve_spike(self, payload: Any) -> None:
        """Accept or reject a high-risk spike. Legal only while in Spike."""
        parsed = self._parsed("approve_spike", payload)
        if parsed is None:
            return
        assert isinstance(parsed, ApproveSpikeRequest)
        if self._state.stage is not ParentStage.SPIKE:
            self._reject(
                "approve_spike",
                f"非法转移: {self._state.stage.value} --approve_spike--> TicketsReady。"
                "当前不在 Spike",
            )
            return
        if self._state.spike_accepted is not None:
            self._reject("approve_spike", "approve_spike 已决定")
            return
        if self._state.waiting != "approve_spike":
            self._reject(
                "approve_spike",
                "当前不在等候 approve_spike：验证过或无需防护时不能批准",
            )
            return
        self._state.spike_accepted = parsed.accepted

    @workflow.signal
    def abort(self, payload: Any) -> None:
        """Request that this Story stop; running children are cancelled by run()."""
        parsed = self._parsed("abort", payload)
        if parsed is None:
            return
        assert isinstance(parsed, AbortRequest)
        if self._state.stage in {ParentStage.DONE, ParentStage.BLOCKED}:
            self._reject("abort", "已结束的执行不能 abort")
            return
        self._state.abort_requested = True
        self._state.abort_reason = parsed.reason
        self._state.ticket_event = True

    @workflow.signal
    def reopen(self, payload: Any) -> None:
        """Accept reopen only at Blocked or Done, then ask run() to start a new execution."""
        parsed = self._parsed("reopen", payload)
        if parsed is None:
            return
        assert isinstance(parsed, ReopenRequest)
        current = self._state.input.execution_id if self._state.input is not None else ""
        if parsed.execution_id == current:
            self._reject("reopen", _REOPEN_SAME_ID)
            return
        if self._state.stage not in {ParentStage.BLOCKED, ParentStage.DONE}:
            self._reject("reopen", _REOPEN_NOT_TERMINAL)
            return
        if self._state.reopen_started or self._state.reopen_request is not None:
            self._reject("reopen", _REOPEN_IN_FLIGHT)
            return
        self._state.reopen_request = parsed
        self._state.reopen_status = f"正在启动新执行 {parsed.execution_id}"

    @workflow.signal
    def promote_rule(self, payload: Any) -> None:
        """Record a promote_rule request. Automatic promotion is always refused."""
        parsed = self._parsed("promote_rule", payload)
        if parsed is None:
            return
        assert isinstance(parsed, PromoteRuleRequest)
        if parsed.automatic:
            self._state.promote_status = _PROMOTE_AUTO_REJECTED
            self._state.record_illegal(_PROMOTE_AUTO_REJECTED)
            return
        self._state.promote_status = _PROMOTE_RECORDED

    @workflow.signal
    def authorize_test_deploy(self, payload: Any = None) -> None:
        """Authorize test-environment publish after all non-dropped tickets verify."""
        parsed = self._parsed("authorize_test_deploy", payload)
        if parsed is None:
            return
        if not can_enter_test_deploying(self._state.ticket_list()):
            self._reject(
                "authorize_test_deploy",
                "尚有 Ticket 未验证过线、仍在飞行阶段，或失败尚未处理，不能授权测试发布",
            )
            return
        if self._state.stage is not ParentStage.IMPLEMENTING:
            self._reject(
                "authorize_test_deploy",
                f"非法转移: {self._state.stage.value} --authorize_test_deploy--> "
                "TestDeploying。当前阶段不能授权测试发布",
            )
            return
        self._state.test_deploy_authorized = True
        self._state.ticket_event = True

    @workflow.signal
    async def review_override(self, payload: Any) -> None:
        """Route a human review decision to a child that is waiting in HumanGate."""
        parsed = self._parsed("review_override", payload)
        if parsed is None:
            return
        assert isinstance(parsed, ReviewOverride)
        snap = self._state.tickets.get(parsed.ticket_id)
        if snap is None:
            self._reject("review_override", f"未知 Ticket: {parsed.ticket_id}")
            return
        if snap.stage is not TicketStage.HUMAN_GATE:
            self._reject(
                "review_override",
                f"Ticket {parsed.ticket_id} 不在 HumanGate，不能 review_override",
            )
            return
        await self._route_to_child(  # type: ignore[attr-defined]
            parsed.ticket_id, "review_override", parsed
        )

    def _parsed(self, signal: str, payload: object) -> object | None:
        parsed, reason = parse_human_signal(signal, payload)
        if reason is not None:
            self._reject(signal, reason)
            return None
        return parsed

    def _reject(self, signal: str, reason: str) -> None:
        self._state.record_rejection(signal, reason)
