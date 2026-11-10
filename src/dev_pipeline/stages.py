"""Parent and child stage names. Identifiers stay in English; values match the docs."""

from enum import StrEnum


class ParentStage(StrEnum):
    """Aggregate stages of one parent DevPipelineWorkflow (one Story)."""

    INTAKE = "Intake"
    SCOPE_LOCKED = "ScopeLocked"
    SCHEME_DECIDED = "SchemeDecided"
    SPIKE = "Spike"
    TICKETS_READY = "TicketsReady"
    IMPLEMENTING = "Implementing"
    TEST_DEPLOYING = "TestDeploying"
    RETRO = "Retro"
    DONE = "Done"
    BLOCKED = "Blocked"


class TicketStage(StrEnum):
    """Stages of one TicketWorkflow (one implementation ticket)."""

    PENDING = "Pending"
    IMPLEMENTING = "Implementing"
    REVIEWING = "Reviewing"
    HUMAN_GATE = "HumanGate"
    VERIFYING = "Verifying"
    VERIFIED = "Verified"
    FAILED = "Failed"
    BLOCKED = "Blocked"
    DROPPED = "Dropped"


class TestDeployStatus(StrEnum):
    """Outcome of the TestDeploying stub publish, visible on the parent snapshot."""

    __test__ = False

    NOT_STARTED = "not_started"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


# Child stages that mean the parent must keep waiting.
IN_FLIGHT_TICKET_STAGES = frozenset(
    {
        TicketStage.IMPLEMENTING,
        TicketStage.REVIEWING,
        TicketStage.HUMAN_GATE,
        TicketStage.VERIFYING,
    }
)

TERMINAL_FAILURE_TICKET_STAGES = frozenset(
    {
        TicketStage.FAILED,
        TicketStage.BLOCKED,
    }
)
