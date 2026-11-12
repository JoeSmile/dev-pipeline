"""Ticket-list completeness, Jira body formatting, and idempotent issue writes.

This module has no network or filesystem I/O. HTTP access lives in
``dev_pipeline.jira_client`` and is called only from Activities.
Workflows may import the pure helpers; they must not call persist functions
that need a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from dev_pipeline.models import PENDING_FIELD, TicketSpec

JIRA_TICKET_PROJECT = "DP"
# Conventional default for issues created in project DP. A later change of
# issue type is 待证; this ticket does not invent a custom field for it.
JIRA_TICKET_ISSUE_TYPE = "Story"
JIRA_TICKET_LABEL = "devpipeline-ticket"
TICKET_ISSUE_PREFIX = "【DevPipeline Ticket】"
TICKET_WRITE_LAG_LABEL = "Jira 写票滞后"


@dataclass
class JiraTicketPersistInput:
    """Activity input for writing the ticket list to Jira project DP."""

    story_id: str
    execution_id: str
    config_root: str
    tickets: list[TicketSpec] = field(default_factory=list)


@dataclass
class JiraTicketPersistResult:
    """Activity result after an idempotent write. Contains no secrets."""

    tickets: list[TicketSpec] = field(default_factory=list)
    created: int = 0
    reused: int = 0


class JiraTicketStore(Protocol):
    """Minimal issue operations needed to persist tickets in project DP."""

    def find_issue_key_by_marker(self, marker: str, project_key: str) -> str | None:
        """Return an existing issue key for the idempotency marker, if any."""

    def create_issue(
        self,
        *,
        project_key: str,
        issue_type: str,
        summary: str,
        description: str,
        labels: list[str],
    ) -> str:
        """Create one issue and return its key."""


def _filled(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value.strip() != PENDING_FIELD


def is_complete_ticket_spec(spec: TicketSpec) -> bool:
    """Return True when 目标, 验收, I/O 契约, and ticket_id are present and not 待证."""

    return (
        _filled(spec.ticket_id)
        and _filled(spec.goal)
        and _filled(spec.acceptance)
        and _filled(spec.io_contract)
        and isinstance(spec.depends_on, list)
    )


def ticket_list_completion_error(specs: list[TicketSpec]) -> str | None:
    """Return a Chinese reason when the list is not complete, or None when it is."""

    if not specs:
        return "Ticket 清单为空，不能视为已齐"
    seen: set[str] = set()
    ids: list[str] = []
    for index, spec in enumerate(specs):
        if not is_complete_ticket_spec(spec):
            return f"Ticket 清单[{index}] 缺少目标、验收、依赖或 I/O 契约，或仍有待证缺项"
        ticket_id = spec.ticket_id.strip()
        if ticket_id in seen:
            return f"Ticket 清单中存在重复的 ticket_id: {ticket_id}"
        seen.add(ticket_id)
        ids.append(ticket_id)
    known = set(ids)
    for spec in specs:
        for dep in spec.depends_on:
            if not isinstance(dep, str) or not dep.strip() or dep.strip() not in known:
                return f"Ticket {spec.ticket_id} 的依赖未知: {dep}"
    if _has_cycle(specs):
        return "Ticket 清单的依赖存在环，不能视为已齐"
    return None


def is_complete_ticket_list(specs: list[TicketSpec]) -> bool:
    """Return True when every required ticket is complete and the graph is valid."""

    return ticket_list_completion_error(specs) is None


def ticket_idempotency_marker(story_id: str, ticket_id: str, execution_id: str) -> str:
    """Return the unique marker stored in a Jira issue description."""

    return f"dp-ticket:{story_id}:{ticket_id}:{execution_id}"


def ticket_issue_summary(spec: TicketSpec) -> str:
    """Return a prefixed Jira summary for one ticket."""

    return f"{TICKET_ISSUE_PREFIX} {spec.ticket_id}: {spec.goal.strip()}"


def ticket_issue_description(spec: TicketSpec, *, story_id: str, execution_id: str) -> str:
    """Return the issue description, including the idempotency marker."""

    deps = "、".join(dep.strip() for dep in spec.depends_on if str(dep).strip()) or "（无）"
    marker = ticket_idempotency_marker(story_id, spec.ticket_id, execution_id)
    return (
        f"{TICKET_ISSUE_PREFIX}\n"
        f"marker: {marker}\n"
        f"ticket_id: {spec.ticket_id}\n"
        f"目标: {spec.goal}\n"
        f"验收: {spec.acceptance}\n"
        f"依赖: {deps}\n"
        f"I/O 契约: {spec.io_contract}\n"
        f"story_id: {story_id}\n"
        f"execution_id: {execution_id}\n"
        "本票由流水线写入 Jira 项目 DP，Jira 不是编排器。"
    )


def persist_tickets(
    store: JiraTicketStore, payload: JiraTicketPersistInput
) -> JiraTicketPersistResult:
    """Create or reuse one Jira DP issue per required ticket. Idempotent on retry."""

    written: list[TicketSpec] = []
    created = 0
    reused = 0
    for spec in payload.tickets:
        marker = ticket_idempotency_marker(payload.story_id, spec.ticket_id, payload.execution_id)
        existing = store.find_issue_key_by_marker(marker, JIRA_TICKET_PROJECT)
        if existing:
            key = existing
            reused += 1
        else:
            key = store.create_issue(
                project_key=JIRA_TICKET_PROJECT,
                issue_type=JIRA_TICKET_ISSUE_TYPE,
                summary=ticket_issue_summary(spec),
                description=ticket_issue_description(
                    spec, story_id=payload.story_id, execution_id=payload.execution_id
                ),
                labels=[JIRA_TICKET_LABEL],
            )
            created += 1
        written.append(
            TicketSpec(
                ticket_id=spec.ticket_id,
                goal=spec.goal,
                acceptance=spec.acceptance,
                depends_on=list(spec.depends_on),
                io_contract=spec.io_contract,
                jira_key=key,
            )
        )
    return JiraTicketPersistResult(tickets=written, created=created, reused=reused)


def _has_cycle(specs: list[TicketSpec]) -> bool:
    graph = {spec.ticket_id.strip(): [dep.strip() for dep in spec.depends_on] for spec in specs}
    visiting: set[str] = set()
    visited: set[str] = set()

    def walk(node: str) -> bool:
        if node in visited:
            return False
        if node in visiting:
            return True
        visiting.add(node)
        for dep in graph.get(node, []):
            if dep in graph and walk(dep):
                return True
        visiting.remove(node)
        visited.add(node)
        return False

    return any(walk(node) for node in graph)
