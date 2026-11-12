"""Serializable inputs, snapshots, and signal payloads for the pipeline workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from dev_pipeline.budget import BudgetSnapshot
from dev_pipeline.constants import TEST_PUBLISH_TARGET
from dev_pipeline.retro import PromotionRecord, RetroRecord
from dev_pipeline.review import DualReviewRecord
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.verify import VerifyRecord

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
    take a ticket off the list after start is ``drop_ticket``. A complete
    list item also carries 目标, 验收, and I/O 契约; ``depends_on`` is 依赖.
    """

    ticket_id: str
    goal: str = ""
    acceptance: str = ""
    depends_on: list[str] = field(default_factory=list)
    io_contract: str = ""
    jira_key: str = ""


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
    sandbox_write: SandboxWriteSpec | None = None


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


SandboxStatus = Literal["unauthorized", "authorized", "allowed", "rejected"]


@dataclass
class SandboxWriteSpec:
    """An explicit coding-sandbox write request reserved until the coding Worker lands.

    Relative paths are resolved against the authorized worktree. Absolute paths
    are still checked against that same boundary. This is not the DP-21
    deliverable list.
    """

    path: str
    content: str = ""


@dataclass
class SandboxRecord:
    """Queryable authorization-boundary outcome on the existing snapshot."""

    authorized_worktree: str = ""
    status: SandboxStatus = "unauthorized"
    requested_path: str = ""
    reason: str = ""


@dataclass
class SandboxWriteInput:
    """Activity input for a guarded sandbox write. Contains no secrets."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str = ""
    path: str = ""
    content: str = ""
    authorized_worktree: str = ""


@dataclass
class SandboxWriteResult:
    """Outcome of a guarded sandbox write. ``allowed`` must not be ignored."""

    allowed: bool
    record: SandboxRecord
    written_path: str = ""


@dataclass
class TicketDeliverables:
    """Queryable coding deliverables for one ticket execution.

    Completeness requires a derivable local branch, a reviewable diff, unit-test
    evidence, and rollback notes. Default is no remote push, merge, or PR.
    ``pr_authorization`` and ``worktree_recycle`` stay 待证 until DP-31 / DP-30.
    """

    branch_name: str = ""
    diff: str = ""
    test_evidence: str = ""
    rollback_notes: str = ""
    complete: bool = False
    tests_ran_in_worktree: bool = False
    worktree: str = ""
    pushed: bool = False
    merged: bool = False
    remote_pr_opened: bool = False
    remote_pr_url: str = ""
    backend: str = "桩"
    pr_authorization: str = PENDING_FIELD
    worktree_recycle: str = PENDING_FIELD
    missing: list[str] = field(default_factory=list)


def is_complete_deliverables(item: TicketDeliverables | None) -> bool:
    """Return True when branch, diff, unit-test evidence, and rollback notes exist."""

    if item is None:
        return False
    return not missing_deliverable_fields(item)


def missing_deliverable_fields(item: TicketDeliverables) -> list[str]:
    """Return the names of missing required deliverable fields."""

    required = (
        ("branch", item.branch_name),
        ("diff", item.diff),
        ("tests", item.test_evidence),
        ("rollback", item.rollback_notes),
    )
    return [name for name, value in required if not (isinstance(value, str) and value.strip())]


@dataclass
class TicketInput:
    """Start payload for one TicketWorkflow child."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str
    parent_workflow_id: str
    depends_on: list[str] = field(default_factory=list)
    sandbox_write: SandboxWriteSpec | None = None
    review_write: SandboxWriteSpec | None = None
    scheme_red_lines: list[str] = field(default_factory=list)
    story_constraints: str = ""
    verify_evidence_path: str = ""
    verify_mode: str = ""


@dataclass
class TicketWorkInput:
    """Shared input for implement / review / verify stub activities."""

    ticket_id: str
    execution_id: str
    story_id: str
    config_root: str = ""


@dataclass
class ImplementResult:
    """Coding deliverable result: branch, diff, unit tests, and rollback notes."""

    diff_submitted: bool = False
    detail: str = ""
    deliverables: TicketDeliverables | None = None


@dataclass
class ReviewFindings:
    """Legacy review list shape. Dual-review snapshots use DualReviewRecord."""

    blocking: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)
    paradigm_drift: bool = False

    def requires_human_gate(self) -> bool:
        """Return True when review findings require a human gate."""
        return bool(self.blocking) or self.paradigm_drift


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
class ReviewOverride:
    """Human review_override signal, routed by the parent to one child.

    ``decision`` is stored as ``approve`` / ``rework`` / ``reject`` after parse;
    the signal also accepts ``放行`` / ``打回`` / ``否决``. The workflow stamps
    ``decided_at`` from ``workflow.now()`` and ignores any client-supplied
    timestamp. ``reason`` is optional.
    """

    ticket_id: str
    decision: ReviewDecision
    decided_by: str = ""
    reason: str = ""
    decided_at: str = ""


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

    Identifiers are derived from ``ticket_id + execution_id``.
    ``authorized_worktree`` is the absolute path the coding sandbox may write
    to. An empty value means unauthorized: no writes are allowed.
    This is not a GitHub PR, and this ticket does not create a Cursor CLI
    ``--worktree`` checkout.
    """

    ticket_id: str
    execution_id: str
    workspace_id: str
    branch_name: str
    pr_token: str
    authorized_worktree: str = ""


@dataclass
class TicketStateUpdate:
    """Child-to-parent progress signal for the queryable snapshot."""

    ticket_id: str
    stage: TicketStage
    unhandled_failure: bool = False
    detail: str = ""
    sandbox: SandboxRecord | None = None
    deliverables: TicketDeliverables | None = None
    review: DualReviewRecord | None = None
    verify: VerifyRecord | None = None


@dataclass
class TicketListSubmission:
    """Payload for the submit_tickets human signal."""

    tickets: list[TicketSpec]
    submitted_by: str = ""


@dataclass
class JiraTicketWriteSnapshot:
    """Queryable Jira ticket-write status on the existing parent snapshot."""

    lagged: bool = False
    lag_marker: str | None = None
    lag_reason: str | None = None
    issue_keys: list[str] = field(default_factory=list)


@dataclass
class TicketSnapshot:
    """One ticket's visible state on the parent snapshot."""

    ticket_id: str
    stage: TicketStage
    depends_on: list[str] = field(default_factory=list)
    goal: str = ""
    acceptance: str = ""
    io_contract: str = ""
    jira_key: str = ""
    started: bool = False
    child_created: bool = False
    unhandled_failure: bool = False
    detail: str = ""
    sandbox: SandboxRecord | None = None
    deliverables: TicketDeliverables | None = None
    review: DualReviewRecord | None = None
    verify: VerifyRecord | None = None


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
    ticket_list_complete: bool = False
    ticket_list_submitted_by: str = ""
    ticket_list_submitted_at: str = ""
    jira_tickets: JiraTicketWriteSnapshot | None = None
    retro: RetroRecord | None = None
    promotion: PromotionRecord | None = None


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
    sandbox: SandboxRecord | None = None
    deliverables: TicketDeliverables | None = None
    review: DualReviewRecord | None = None
    verify: VerifyRecord | None = None


def ticket_workflow_id(ticket_id: str, execution_id: str) -> str:
    """Idempotency key used as the child workflow id: ticket_id + execution_id."""

    return f"{ticket_id}+{execution_id}"


def story_workflow_id(story_id: str, execution_id: str) -> str:
    """Workflow id for one parent Story execution."""

    return f"story-{story_id}-{execution_id}"
