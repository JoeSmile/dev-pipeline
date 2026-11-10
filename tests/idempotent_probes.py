"""Test-only workflows that exercise idempotent child start / attach."""

from __future__ import annotations

from typing import Any

from temporalio import workflow

from dev_pipeline.models import TicketInput, TicketResult
from dev_pipeline.workflows.child_start import start_or_attach_ticket_child
from dev_pipeline.workflows.ticket import TicketWorkflow


@workflow.defn
class ReentryChildStartWorkflow:
    """Start-or-attach the same ticket child twice without waiting for it to finish."""

    @workflow.run
    async def run(self, payload: TicketInput) -> dict[str, Any]:
        first, started_first = await start_or_attach_ticket_child(payload)
        second, started_second = await start_or_attach_ticket_child(payload)
        if started_first:
            await first.signal(TicketWorkflow.dependencies_satisfied)
        return {
            "first_id": first.id,
            "second_id": second.id,
            "started_first": started_first,
            "started_second": started_second,
        }


@workflow.defn
class CompletedChildReuseWorkflow:
    """Start a ticket child, wait for its result, then attach instead of starting again."""

    @workflow.run
    async def run(self, payload: TicketInput) -> dict[str, Any]:
        first, started_first = await start_or_attach_ticket_child(payload)
        if started_first:
            await first.signal(TicketWorkflow.dependencies_satisfied)
        result: TicketResult = await first  # type: ignore[misc]
        second, started_second = await start_or_attach_ticket_child(payload)
        return {
            "first_id": first.id,
            "second_id": second.id,
            "started_first": started_first,
            "started_second": started_second,
            "stage": result.stage.value,
            "detail": result.detail,
        }
