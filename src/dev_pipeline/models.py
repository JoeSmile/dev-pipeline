"""Serializable inputs, snapshots, and signal payloads for the pipeline workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from dev_pipeline.budget import BudgetSnapshot
from dev_pipeline.constants import TEST_PUBLISH_TARGET
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage

ReviewDecision = Literal["approve", "rework", "reject"]
PublishTarget = Literal["test"]


PENDING_FIELD = "待证"
INTAKE_SOURCE_CLI = "cli"


@dataclass
class IntakeRecord:
    """Shared Intake shape for CLI start and a later label-listener path.

    Missing fields are marked ``待证``. CLI fills ``source=cli`` and leaves
    reporter, request, and auto-run label unused until those tickets land.
    """

    jira_key: str
    reporter: str = PENDING_FIELD
    request: str = PENDING_FIELD
    source: str = PENDING_FIELD
    has_auto_run_label: str = PENDING_FIELD


@dataclass
class StoryDraft:
    """Normalized Story draft produced during Intake.

    Missing fields are marked ``待证``. This draft is the input to
    ``lock_scope``; locking itself is done by the human signal, not by
    draft normalization.
    """

    to_solve: str = PENDING_FIELD
    known_constraints: str = PENDING_FIELD
    missing_items: list[str] = field(default_factory=list)
    out_of_scope: str = PENDING_FIELD
    acceptance: str = PENDING_FIELD
    complete: bool = False


@dataclass
class StoryDraftInput:
    """Activity input for Intake story normalization. Contains no secrets."""

    intake: IntakeRecord | None = None
    summary: str = ""
    config_root: str = ""


@dataclass
class TicketSpec:
    """One implementation ticket on a Story, including dependency edges.

    Every ticket in the start payload is required. The only in-scope way to
    take a ticket off the list after start is ``drop_ticket``.
    """

    ticket_id: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class ScopeLock:
    """lock_scope payload and the locked-scope execution record.

    Human signals must supply ``in_scope``, ``out_of_scope``, and
    ``locked_by``. The workflow stamps ``locked_at`` from
    ``workflow.now()`` and ignores any client-supplied timestamp.
    """

    in_scope: str
    out_of_scope: str = ""
    locked_by: str = ""
    locked_at: str = ""


def format_scope_lock_time(moment: datetime) -> str:
    """Format a lock timestamp as UTC ISO-8601 with a ``Z`` suffix."""

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def is_complete_scope_lock(scope: ScopeLock) -> bool:
    """Return True when 要解决, 不做什么, 锁定人, and 时间 are all non-empty."""

    return all(
        isinstance(value, str) and bool(value.strip())
        for value in (scope.in_scope, scope.out_of_scope, scope.locked_by, scope.locked_at)
    )


def has_red_line_hypothesis(hypotheses: list[SchemeHypothesis]) -> bool:
    """Return True when at least one hypothesis is graded 红线级."""

    return any(item.grade == "红线级" for item in hypotheses)


def is_complete_scheme_decision(scheme: SchemeDecision) -> bool:
    """Return True when 取舍说明, 假设清单, 拍板人, 时间, and 检索标记 are present."""

    if not all(
        isinstance(value, str) and bool(value.strip())
        for value in (scheme.tradeoff, scheme.decided_by, scheme.decided_at)
    ):
        return False
    if scheme.retrieval not in {RETRIEVAL_STUB, RETRIEVAL_HIT}:
        return False
    if not isinstance(scheme.hypotheses, list):
        return False
    return all(
        isinstance(item.statement, str)
        and bool(item.statement.strip())
        and item.grade in HYPOTHESIS_GRADES
        for item in scheme.hypotheses
    )


@dataclass
class StoryInput:
    """Start payload for one parent DevPipelineWorkflow execution."""

    story_id: str
    execution_id: str
    config_root: str
    tickets: list[TicketSpec] = field(default_factory=list)
    summary: str = ""
    intake: IntakeRecord | None = None
    inherited_scope: ScopeLock | None = None
    auto_start_children: bool = False


HypothesisGrade = Literal["红线级", "局部", "候选"]
RetrievalMark = Literal["桩", "真实命中"]
HYPOTHESIS_GRADES: frozenset[HypothesisGrade] = frozenset({"红线级", "局部", "候选"})
RETRIEVAL_STUB: RetrievalMark = "桩"
RETRIEVAL_HIT: RetrievalMark = "真实命中"


@dataclass
class SchemeHypothesis:
    """One graded scheme hypothesis. Grade must be 红线级 / 局部 / 候选."""

    statement: str
    grade: HypothesisGrade


@dataclass
class SchemeDecision:
    """decide_scheme payload and the scheme trade-off execution record.

    Human signals must supply ``tradeoff``, ``hypotheses``, and
    ``decided_by``. The workflow stamps ``decided_at`` from
    ``workflow.now()`` and records ``retrieval`` as ``桩`` on the
    deterministic stub path, ignoring client-supplied time or a claimed
    ``真实命中``. ``has_high_risk_assumption`` is derived from whether
    any hypothesis is graded 红线级.
    """

    tradeoff: str
    hypotheses: list[SchemeHypothesis] = field(default_factory=list)
    decided_by: str = ""
    decided_at: str = ""
    retrieval: RetrievalMark = RETRIEVAL_STUB
    tickets: list[TicketSpec] = field(default_factory=list)
    has_high_risk_assumption: bool = False
    notes: str = ""


SpikeOutcome = Literal["验证过", "击穿红线"]
SpikeConclusion = Literal["验证过", "击穿红线", "超时未验证"]
SPIKE_VERIFIED: SpikeOutcome = "验证过"
SPIKE_BROKEN: SpikeOutcome = "击穿红线"
SPIKE_OVERRUN: SpikeConclusion = "超时未验证"
SPIKE_BACKEND_STUB = "桩"
# 待证占位时限：4 小时，落在票面「半小时到半天」示例窗口内，不是已签批的正式 SLA。
DEFAULT_SPIKE_TIMEOUT_SECONDS = 4 * 60 * 60


def red_line_statements(hypotheses: list[SchemeHypothesis]) -> str:
    """Join 红线级 hypothesis statements into one falsifiable claim."""

    statements = [
        item.statement.strip()
        for item in hypotheses
        if item.grade == "红线级" and item.statement.strip()
    ]
    return "；".join(statements) if statements else PENDING_FIELD


@dataclass
class SpikeConfig:
    """Duration bound loaded from optional spike.toml, or the 待证 default."""

    timeout_seconds: int = DEFAULT_SPIKE_TIMEOUT_SECONDS
    source: str = "待证默认"


@dataclass
class SpikeInput:
    """Spike activity input. The runner must not write target-story code."""

    story_id: str
    execution_id: str
    config_root: str = ""
    statement: str = PENDING_FIELD
    budget_seconds: int = DEFAULT_SPIKE_TIMEOUT_SECONDS


@dataclass
class SpikeResult:
    """Falsifiable spike conclusion produced by the deterministic stub backend.

    The activity never records 超时未验证; the workflow stamps that when the
    duration bound is exceeded. ``red_line_failed`` stays aligned with
    ``conclusion == 击穿红线`` for older test stubs.
    """

    conclusion: SpikeOutcome = SPIKE_VERIFIED
    statement: str = ""
    detail: str = ""
    has_protection: bool = False
    backend: str = SPIKE_BACKEND_STUB
    red_line_failed: bool = False


@dataclass
class SpikeRecord:
    """Queryable spike conclusion on the existing parent snapshot."""

    conclusion: SpikeConclusion
    statement: str
    detail: str
    has_protection: bool = False
    timed_out: bool = False
    budget_seconds: int = DEFAULT_SPIKE_TIMEOUT_SECONDS
    started_at: str = ""
    finished_at: str = ""
    backend: str = SPIKE_BACKEND_STUB
    source: str = "待证默认"


@dataclass
class TicketInput:
    """Start payload for one TicketWorkflow child."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str
    parent_workflow_id: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class TicketWorkInput:
    """Shared input for implement / review / verify stub activities."""

    ticket_id: str
    execution_id: str
    story_id: str


@dataclass
class ImplementResult:
    """Stub coding result. Real Coding workers are out of scope."""

    diff_submitted: bool = True
    detail: str = "stub-implement"


@dataclass
class ReviewFindings:
    """Review stub output. Blocking findings or paradigm drift must HumanGate."""

    blocking: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    paradigm_drift: bool = False

    def requires_human_gate(self) -> bool:
        """Return True when review findings require a human gate."""
        return bool(self.blocking) or self.paradigm_drift


@dataclass
class VerifyResult:
    """Stub verification result. Failure returns the child to Implementing."""

    passed: bool = True
    evidence: str = ""


@dataclass
class PublishRequest:
    """Test-environment publish request. Any production target must be rejected."""

    story_id: str
    execution_id: str
    target: PublishTarget = TEST_PUBLISH_TARGET


@dataclass
class PublishResult:
    """Reachable test-env address, or a failed publish that must not enter Retro."""

    status: Literal["succeeded", "failed"] = "succeeded"
    address: str = ""
    detail: str = ""


@dataclass
class RetroInput:
    """Stub retro activity input. The stub must not write files."""

    story_id: str
    execution_id: str


@dataclass
class RetroResult:
    """Stub retro result. Real daily/candidate writes are later tickets."""

    recorded: bool = True
    detail: str = "stub-retro"


@dataclass
class ReviewOverride:
    """Human review_override signal, routed by the parent to one child."""

    ticket_id: str
    decision: ReviewDecision


@dataclass
class PromoteRuleRequest:
    """Human promote_rule signal. Automatic promotion is always rejected."""

    rule_name: str
    automatic: bool = False


@dataclass
class ApproveSpikeRequest:
    """Payload for the approve_spike human signal."""

    accepted: bool


@dataclass
class AbortRequest:
    """Payload for the abort human signal."""

    reason: str


@dataclass
class ReopenRequest:
    """Payload for the reopen human signal. Must carry a new execution_id."""

    execution_id: str


@dataclass
class AuthorizeTestDeployRequest:
    """Payload for authorize_test_deploy. This signal has no required fields."""


@dataclass
class SignalRejection:
    """Queryable record of a rejected human signal. The current stage is unchanged."""

    signal: str
    reason: str


@dataclass
class ReopenStartInput:
    """Safe start payload for a new parent execution. Contains no secrets."""

    story_id: str
    execution_id: str
    config_root: str
    task_queue: str
    tickets: list[TicketSpec] = field(default_factory=list)
    summary: str = ""
    intake: IntakeRecord | None = None
    inherited_scope: ScopeLock | None = None
    auto_start_children: bool = False


@dataclass
class ReopenStartResult:
    """Outcome of starting a reopened parent execution."""

    started: bool
    workflow_id: str
    execution_id: str
    error: str | None = None


@dataclass
class WorkspaceClaim:
    """Recorded workspace / branch / PR-like identity for one ticket execution.

    This is a side-effect claim, not a real git worktree or GitHub PR.
    Identifiers are derived from ``ticket_id + execution_id``.
    """

    ticket_id: str
    execution_id: str
    workspace_id: str
    branch_name: str
    pr_token: str


@dataclass
class TicketStateUpdate:
    """Child-to-parent progress signal for the queryable snapshot."""

    ticket_id: str
    stage: TicketStage
    unhandled_failure: bool = False
    detail: str = ""


@dataclass
class TicketSnapshot:
    """One ticket's visible state on the parent snapshot."""

    ticket_id: str
    stage: TicketStage
    depends_on: list[str] = field(default_factory=list)
    started: bool = False
    unhandled_failure: bool = False
    detail: str = ""


@dataclass
class JiraMirrorSnapshot:
    """Queryable Jira mirror status on the existing parent snapshot (DP-14 / DP-27)."""

    last_mirrored_stage: str | None = None
    lagged: bool = False
    lag_marker: str | None = None
    lag_reason: str | None = None


@dataclass
class PipelineSnapshot:
    """Thin queryable progress view (DP-47): stage, waiting signal, tickets, deploy."""

    story_id: str
    execution_id: str
    stage: ParentStage
    waiting_signal: str | None
    tickets: list[TicketSnapshot] = field(default_factory=list)
    test_deploy_status: TestDeployStatus = TestDeployStatus.NOT_STARTED
    blocked_reason: str | None = None
    last_illegal: str | None = None
    last_rejection: SignalRejection | None = None
    promote_rule_status: str | None = None
    test_deploy_address: str | None = None
    reopened_execution_id: str | None = None
    reopen_status: str | None = None
    budget: BudgetSnapshot | None = None
    intake: IntakeRecord | None = None
    jira_mirror: JiraMirrorSnapshot | None = None
    story_draft: StoryDraft | None = None
    scope: ScopeLock | None = None
    scheme: SchemeDecision | None = None
    spike: SpikeRecord | None = None


@dataclass
class PipelineResult:
    """Terminal result of a parent execution (Done or Blocked)."""

    story_id: str
    execution_id: str
    stage: ParentStage
    test_deploy_status: TestDeployStatus
    blocked_reason: str | None = None
    test_deploy_address: str | None = None


@dataclass
class TicketResult:
    """Terminal or parked result of a child execution."""

    ticket_id: str
    execution_id: str
    stage: TicketStage
    detail: str = ""


def ticket_workflow_id(ticket_id: str, execution_id: str) -> str:
    """Idempotency key used as the child workflow id: ticket_id + execution_id."""

    return f"{ticket_id}+{execution_id}"


def story_workflow_id(story_id: str, execution_id: str) -> str:
    """Workflow id for one parent Story execution."""

    return f"story-{story_id}-{execution_id}"
