"""Unit tests for parent snapshot wait-signal refresh."""

from dev_pipeline.constants import WAITING_AUTHORIZE_TEST_DEPLOY
from dev_pipeline.models import TicketSpec
from dev_pipeline.stages import TicketStage
from dev_pipeline.workflows.parent_state import ParentState


def _state(*ticket_ids: str) -> ParentState:
    state = ParentState()
    state.install_tickets([TicketSpec(ticket_id=ticket_id) for ticket_id in ticket_ids])
    return state


def test_refresh_ticket_wait_sets_authorize_after_drop_when_sibling_verified() -> None:
    state = _state("A", "B")
    state.tickets["A"].stage = TicketStage.FAILED
    state.tickets["A"].unhandled_failure = True
    state.tickets["B"].stage = TicketStage.VERIFIED
    state.refresh_ticket_wait()
    assert state.waiting == "retry_ticket_or_drop_ticket_or_abort"

    state.tickets["A"].stage = TicketStage.DROPPED
    state.tickets["A"].unhandled_failure = False
    state.refresh_ticket_wait()
    assert state.waiting == WAITING_AUTHORIZE_TEST_DEPLOY


def test_refresh_ticket_wait_stays_clear_while_a_sibling_is_in_flight() -> None:
    state = _state("A", "B")
    state.tickets["A"].stage = TicketStage.FAILED
    state.tickets["A"].unhandled_failure = True
    state.tickets["B"].stage = TicketStage.VERIFYING
    state.refresh_ticket_wait()
    assert state.waiting is None
