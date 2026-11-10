"""DP-26 topology: independent tickets may start together; dependents must wait."""

from dev_pipeline.models import TicketSnapshot, TicketSpec
from dev_pipeline.stages import TicketStage
from dev_pipeline.topology import eligible_ticket_ids, is_eligible_to_start, would_start_early


def _snap(
    ticket_id: str,
    stage: TicketStage,
    *,
    started: bool = False,
    depends_on: list[str] | None = None,
) -> TicketSnapshot:
    return TicketSnapshot(
        ticket_id=ticket_id,
        stage=stage,
        required=True,
        depends_on=depends_on or [],
        started=started,
    )


def test_independent_tickets_are_eligible_together() -> None:
    specs = [TicketSpec(ticket_id="A"), TicketSpec(ticket_id="B")]
    snapshots = [
        _snap("A", TicketStage.PENDING),
        _snap("B", TicketStage.PENDING),
    ]
    assert eligible_ticket_ids(specs, snapshots) == ("A", "B")


def test_dependent_ticket_cannot_start_early() -> None:
    specs = [
        TicketSpec(ticket_id="A"),
        TicketSpec(ticket_id="B", depends_on=["A"]),
    ]
    pending = [
        _snap("A", TicketStage.PENDING),
        _snap("B", TicketStage.PENDING, depends_on=["A"]),
    ]
    assert eligible_ticket_ids(specs, pending) == ("A",)
    assert would_start_early(specs[1], {"A": TicketStage.PENDING, "B": TicketStage.PENDING})
    assert not is_eligible_to_start(
        specs[1],
        {"A": TicketStage.IMPLEMENTING, "B": TicketStage.PENDING},
        already_started=False,
    )
    a_reviewing = {"A": TicketStage.REVIEWING, "B": TicketStage.PENDING}
    assert eligible_ticket_ids(
        specs,
        [
            _snap("A", TicketStage.REVIEWING, started=True),
            _snap("B", TicketStage.PENDING, depends_on=["A"]),
        ],
    ) == ()
    assert would_start_early(specs[1], a_reviewing)
    verified = {"A": TicketStage.VERIFIED, "B": TicketStage.PENDING}
    assert is_eligible_to_start(specs[1], verified, already_started=False)
    assert not would_start_early(specs[1], verified)
