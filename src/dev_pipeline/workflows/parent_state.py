"""Mutable parent-run state and snapshot helpers. Must stay deterministic."""

from __future__ import annotations

from dev_pipeline.budget import (
    BudgetAmounts,
    BudgetBreach,
    BudgetConfig,
    BudgetSnapshot,
    build_budget_snapshot,
)
from dev_pipeline.constants import WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.jira_mirror import LAG_MARKER_LABEL
from dev_pipeline.models import (
    JiraMirrorSnapshot,
    JiraTicketWriteSnapshot,
    PipelineResult,
    PipelineSnapshot,
    ReopenRequest,
    SchemeDecision,
    ScopeLock,
    SignalRejection,
    SpikeRecord,
    StoryDraft,
    StoryInput,
    TicketSnapshot,
    TicketSpec,
)
from dev_pipeline.stages import IN_FLIGHT_TICKET_STAGES, ParentStage, TestDeployStatus, TicketStage
from dev_pipeline.ticket_list import TICKET_WRITE_LAG_LABEL, is_complete_ticket_list
from dev_pipeline.transitions import (
    ParentGuardContext,
    assert_legal_transition,
    can_enter_test_deploying,
    unhandled_failures,
)


class ParentState:
    """Queryable state for one DevPipelineWorkflow execution."""

    def __init__(self) -> None:
        self.stage = ParentStage.INTAKE
        self.waiting: str | None = "lock_scope"
        self.input: StoryInput | None = None
        self.scope: ScopeLock | None = None
        self.scheme: SchemeDecision | None = None
        self.spike: SpikeRecord | None = None
        self.spike_accepted: bool | None = None
        self.abort_requested = False
        self.abort_reason = "abort"
        self.blocked_reason: str | None = None
        self.last_illegal: str | None = None
        self.last_rejection: SignalRejection | None = None
        self.promote_status: str | None = None
        self.reopen_request: ReopenRequest | None = None
        self.reopen_started = False
        self.reopened_execution_id: str | None = None
        self.reopen_status: str | None = None
        self.test_deploy_status = TestDeployStatus.NOT_STARTED
        self.test_deploy_address: str | None = None
        self.test_deploy_authorized = False
        self.tickets: dict[str, TicketSnapshot] = {}
        self.specs: list[TicketSpec] = []
        self.ticket_event = False
        self.budget_config: BudgetConfig | None = None
        self.budget_spent = BudgetAmounts()
        self.budget_stopped_paths: list[str] = []
        self.budget_no_new_evidence_paths: list[str] = []
        self.budget_last_breach: BudgetBreach | None = None
        self.budget_last_stop_reason: str | None = None
        self.costly_busy = False
        self.jira_mirror: JiraMirrorSnapshot | None = None
        self.story_draft: StoryDraft | None = None
        self.ticket_list_submitted_by = ""
        self.ticket_list_submitted_at = ""
        self.jira_tickets: JiraTicketWriteSnapshot | None = None

    def install_tickets(self, specs: list[TicketSpec]) -> None:
        """Replace the ticket list from a start payload or scheme decision."""
        if not specs:
            return
        self.specs = list(specs)
        self.tickets = {
            spec.ticket_id: TicketSnapshot(
                ticket_id=spec.ticket_id,
                stage=TicketStage.PENDING,
                depends_on=list(spec.depends_on),
                goal=spec.goal,
                acceptance=spec.acceptance,
                io_contract=spec.io_contract,
                jira_key=spec.jira_key,
                started=False,
                child_created=False,
            )
            for spec in specs
        }
        self.ticket_event = True

    def install_budget(self, config: BudgetConfig) -> None:
        """Install the loaded budget config and reset spent / stop records."""
        self.budget_config = config
        self.budget_spent = BudgetAmounts()
        self.budget_stopped_paths = []
        self.budget_no_new_evidence_paths = []
        self.budget_last_breach = None
        self.budget_last_stop_reason = None
        self.costly_busy = False

    def ticket_list(self) -> list[TicketSnapshot]:
        """Return ticket snapshots in the installed spec order."""
        return [
            self.tickets[spec.ticket_id] for spec in self.specs if spec.ticket_id in self.tickets
        ]

    def guard_ctx(self) -> ParentGuardContext:
        """Build the parent guard context from the current snapshot."""
        return ParentGuardContext(
            tickets=tuple(self.ticket_list()),
            test_deploy_status=self.test_deploy_status,
        )

    def has_in_flight(self) -> bool:
        """Return True when a non-dropped ticket is still in a working stage."""
        return any(
            not item.unhandled_failure and item.stage in IN_FLIGHT_TICKET_STAGES
            for item in self.ticket_list()
        )

    def refresh_ticket_wait(self) -> None:
        """Update waiting_signal from the current ticket list and deploy flag."""
        if can_enter_test_deploying(self.ticket_list()) and not self.test_deploy_authorized:
            self.waiting = WAITING_AUTHORIZE_TEST_DEPLOY
            return
        if unhandled_failures(self.ticket_list()) and not self.has_in_flight():
            self.waiting = "retry_ticket_or_drop_ticket_or_abort"
            return
        self.waiting = None

    def move(self, dest: ParentStage, trigger: str) -> None:
        """Apply a legal parent transition, or no-op when already at dest."""
        if self.stage is dest:
            return
        assert_legal_transition(
            self.stage.value,
            dest.value,
            trigger,
            scope="parent",
            ctx=self.guard_ctx(),
        )
        self.stage = dest

    def record_illegal(self, message: str) -> None:
        """Record an operator-visible illegal request on the snapshot."""
        self.last_illegal = message

    def record_rejection(self, signal: str, reason: str) -> None:
        """Record an explicit signal rejection without changing the current stage."""
        self.last_rejection = SignalRejection(signal=signal, reason=reason)
        self.last_illegal = reason

    def finish_blocked(self, reason: str) -> PipelineResult:
        """Mark the parent Blocked and return the terminal result."""
        self.stage = ParentStage.BLOCKED
        self.waiting = None
        self.blocked_reason = reason
        return self.result()

    def result(self) -> PipelineResult:
        """Build the terminal parent result from current state."""
        assert self.input is not None
        return PipelineResult(
            story_id=self.input.story_id,
            execution_id=self.input.execution_id,
            stage=self.stage,
            test_deploy_status=self.test_deploy_status,
            blocked_reason=self.blocked_reason,
            test_deploy_address=self.test_deploy_address,
        )

    def jira_issue_key(self) -> str | None:
        """Return the Intake Jira key when one is present on the start payload."""
        if self.input is None or self.input.intake is None:
            return None
        key = self.input.intake.jira_key.strip()
        return key or None

    def record_jira_mirror_success(self, stage: str) -> None:
        """Record a successful stage mirror and clear any previous lag."""
        self.jira_mirror = JiraMirrorSnapshot(
            last_mirrored_stage=stage,
            lagged=False,
            lag_marker=None,
            lag_reason=None,
        )

    def record_jira_mirror_lag(self, reason: str) -> None:
        """Record 镜像滞后 without changing the current parent stage."""
        last = None if self.jira_mirror is None else self.jira_mirror.last_mirrored_stage
        self.jira_mirror = JiraMirrorSnapshot(
            last_mirrored_stage=last,
            lagged=True,
            lag_marker=LAG_MARKER_LABEL,
            lag_reason=reason,
        )

    def budget_view(self) -> BudgetSnapshot | None:
        """Build the queryable budget view, or None before config is loaded."""
        if self.budget_config is None:
            return None
        return build_budget_snapshot(
            self.budget_config,
            self.budget_spent,
            stopped_paths=self.budget_stopped_paths,
            no_new_evidence_paths=self.budget_no_new_evidence_paths,
            last_breach=self.budget_last_breach,
            last_stop_reason=self.budget_last_stop_reason,
        )

    def record_jira_ticket_success(self, tickets: list[TicketSpec]) -> None:
        """Record successful Jira ticket writes and copy issue keys onto snapshots."""
        keys: list[str] = []
        by_id = {spec.ticket_id: spec for spec in tickets}
        for spec in self.specs:
            written = by_id.get(spec.ticket_id)
            if written is None or not written.jira_key:
                continue
            spec.jira_key = written.jira_key
            keys.append(written.jira_key)
            snap = self.tickets.get(spec.ticket_id)
            if snap is not None:
                snap.jira_key = written.jira_key
        self.jira_tickets = JiraTicketWriteSnapshot(
            lagged=False,
            lag_marker=None,
            lag_reason=None,
            issue_keys=keys,
        )

    def record_jira_ticket_lag(self, reason: str) -> None:
        """Record 写票滞后 without changing the current parent stage."""
        last = [] if self.jira_tickets is None else list(self.jira_tickets.issue_keys)
        self.jira_tickets = JiraTicketWriteSnapshot(
            lagged=True,
            lag_marker=TICKET_WRITE_LAG_LABEL,
            lag_reason=reason,
            issue_keys=last,
        )

    def snapshot(self) -> PipelineSnapshot:
        """Build the queryable progress snapshot."""
        story_id = self.input.story_id if self.input is not None else ""
        execution_id = self.input.execution_id if self.input is not None else ""
        intake = self.input.intake if self.input is not None else None
        return PipelineSnapshot(
            story_id=story_id,
            execution_id=execution_id,
            stage=self.stage,
            waiting_signal=self.waiting,
            tickets=self.ticket_list(),
            test_deploy_status=self.test_deploy_status,
            blocked_reason=self.blocked_reason,
            last_illegal=self.last_illegal,
            last_rejection=self.last_rejection,
            promote_rule_status=self.promote_status,
            test_deploy_address=self.test_deploy_address,
            reopened_execution_id=self.reopened_execution_id,
            reopen_status=self.reopen_status,
            budget=self.budget_view(),
            intake=intake,
            jira_mirror=self.jira_mirror,
            story_draft=self.story_draft,
            scope=self.scope,
            scheme=self.scheme,
            spike=self.spike,
            ticket_list_complete=is_complete_ticket_list(self.specs),
            ticket_list_submitted_by=self.ticket_list_submitted_by,
            ticket_list_submitted_at=self.ticket_list_submitted_at,
            jira_tickets=self.jira_tickets,
        )
