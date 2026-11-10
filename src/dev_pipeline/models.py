"""Serializable inputs, snapshots, and signal payloads for the pipeline workflows."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from dev_pipeline.constants import TEST_PUBLISH_TARGET
from dev_pipeline.stages import ParentStage, TestDeployStatus, TicketStage

ReviewDecision = Literal["approve", "rework", "reject"]
PublishTarget = Literal["test"]


@dataclass
class TicketSpec:
    """One implementation ticket on a Story, including dependency edges.

    Every ticket in the start payload is required. The only in-scope way to
    take a ticket off the list after start is ``drop_ticket``.
    """

    ticket_id: str
    depends_on: list[str] = field(default_factory=list)


@dataclass
class StoryInput:
    """Start payload for one parent DevPipelineWorkflow execution."""

    story_id: str
    execution_id: str
    config_root: str
    tickets: list[TicketSpec] = field(default_factory=list)
    summary: str = ""


@dataclass
class ScopeLock:
    """Payload for the lock_scope human signal."""

    in_scope: str
    out_of_scope: str = ""


@dataclass
class SchemeDecision:
    """Payload for the decide_scheme human signal."""

    has_high_risk_assumption: bool = False
    tickets: list[TicketSpec] = field(default_factory=list)
    notes: str = ""


@dataclass
class SpikeInput:
    """Stub spike activity input."""

    story_id: str
    execution_id: str


@dataclass
class SpikeResult:
    """Stub spike activity result. A red-line failure blocks the Story."""

    red_line_failed: bool = False
    detail: str = ""


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


@dataclass
class ReopenStartResult:
    """Outcome of starting a reopened parent execution."""

    started: bool
    workflow_id: str
    execution_id: str
    error: str | None = None


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
