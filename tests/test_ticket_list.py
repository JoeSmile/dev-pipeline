"""Unit tests for ticket-list completeness and idempotent Jira writes."""

from __future__ import annotations

from pathlib import Path

from dev_pipeline.jira_mirror import InMemoryJira
from dev_pipeline.models import PENDING_FIELD, TicketSpec
from dev_pipeline.ticket_list import (
    JIRA_TICKET_PROJECT,
    JiraTicketPersistInput,
    is_complete_ticket_list,
    is_complete_ticket_spec,
    persist_tickets,
    ticket_idempotency_marker,
    ticket_list_completion_error,
)


def _complete(ticket_id: str, depends_on: list[str] | None = None) -> TicketSpec:
    return TicketSpec(
        ticket_id=ticket_id,
        goal=f"实现 {ticket_id}",
        acceptance=f"{ticket_id} 验收",
        depends_on=list(depends_on or []),
        io_contract=f"{ticket_id} I/O",
    )


def test_complete_item_requires_goal_acceptance_deps_and_io_contract() -> None:
    spec = _complete("A")
    assert is_complete_ticket_spec(spec)
    assert is_complete_ticket_list([spec])
    assert ticket_list_completion_error([spec]) is None


def test_empty_or_pending_fields_are_incomplete() -> None:
    assert not is_complete_ticket_list([])
    assert ticket_list_completion_error([]) is not None
    bare = TicketSpec(ticket_id="A")
    assert not is_complete_ticket_spec(bare)
    pending = TicketSpec(
        ticket_id="A",
        goal=PENDING_FIELD,
        acceptance="验收",
        io_contract="I/O",
    )
    assert not is_complete_ticket_spec(pending)
    missing_io = TicketSpec(ticket_id="A", goal="目标", acceptance="验收", io_contract="")
    assert ticket_list_completion_error([missing_io]) is not None


def test_duplicate_ids_unknown_deps_and_cycles_are_incomplete() -> None:
    assert "重复" in (ticket_list_completion_error([_complete("A"), _complete("A")]) or "")
    assert "未知" in (ticket_list_completion_error([_complete("B", depends_on=["A"])]) or "")
    cyclic = ticket_list_completion_error(
        [_complete("A", depends_on=["B"]), _complete("B", depends_on=["A"])]
    )
    assert cyclic is not None and "环" in cyclic


def test_persist_tickets_is_idempotent_and_does_not_write_tasks_md(tmp_path: Path) -> None:
    store = InMemoryJira()
    payload = JiraTicketPersistInput(
        story_id="STORY-1",
        execution_id="exec-1",
        config_root=str(tmp_path),
        tickets=[_complete("A"), _complete("B", depends_on=["A"])],
    )
    first = persist_tickets(store, payload)
    second = persist_tickets(store, payload)
    assert first.created == 2
    assert first.reused == 0
    assert second.created == 0
    assert second.reused == 2
    assert [item.jira_key for item in first.tickets] == [item.jira_key for item in second.tickets]
    keys = {item.jira_key for item in first.tickets}
    assert keys == set(store.issues())
    for issue in store.issues().values():
        assert issue["project"] == JIRA_TICKET_PROJECT
        ticket_id = "A" if "A" in str(issue["summary"]) else "B"
        marker = ticket_idempotency_marker("STORY-1", ticket_id, "exec-1")
        assert marker in str(issue["description"])
        assert "目标:" in str(issue["description"])
        assert "验收:" in str(issue["description"])
        assert "I/O 契约:" in str(issue["description"])
    assert not (tmp_path / "TASKS.md").exists()
    assert not (tmp_path / "TASKS.md").is_file()
