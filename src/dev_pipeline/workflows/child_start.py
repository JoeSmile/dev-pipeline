"""Idempotent TicketWorkflow child start / attach helpers."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.workflow import (
    ChildWorkflowCancellationType,
    ChildWorkflowHandle,
    ExternalWorkflowHandle,
    ParentClosePolicy,
)

from dev_pipeline.models import TicketInput, TicketResult, ticket_workflow_id
from dev_pipeline.workflows.ticket import TicketWorkflow

CHILD_EXECUTION_TIMEOUT = timedelta(hours=24)
CHILD_RETRY = RetryPolicy(maximum_attempts=1)
CHILD_ID_REUSE_POLICY = WorkflowIDReusePolicy.REJECT_DUPLICATE

TicketChildHandle = ChildWorkflowHandle[Any, TicketResult] | ExternalWorkflowHandle[Any]


async def start_or_attach_ticket_child(
    payload: TicketInput,
    *,
    parent_close_policy: ParentClosePolicy = ParentClosePolicy.TERMINATE,
    cancellation_type: ChildWorkflowCancellationType = (
        ChildWorkflowCancellationType.TRY_CANCEL
    ),
    execution_timeout: timedelta = CHILD_EXECUTION_TIMEOUT,
    retry_policy: RetryPolicy = CHILD_RETRY,
) -> tuple[TicketChildHandle, bool]:
    """Start a child TicketWorkflow, or attach when the same id already exists.

    The child workflow id is the idempotency key ``ticket_id+execution_id``.
    ``REJECT_DUPLICATE`` prevents a completed or failed id from starting a new
    run. A second start attempt attaches to the existing execution instead of
    failing the parent or creating a second child.

    Args:
        payload: Child start input, including ticket_id and execution_id.
        parent_close_policy: Policy applied only to a newly started child.
        cancellation_type: Cancellation type applied only to a newly started child.
        execution_timeout: Child execution timeout for a new start.
        retry_policy: Child retry policy for a new start (no new execution_id).

    Returns:
        The child or external handle, and True when this call started a new child.
    """

    child_id = ticket_workflow_id(payload.ticket_id, payload.execution_id)
    try:
        handle = await workflow.start_child_workflow(
            TicketWorkflow.run,
            payload,
            id=child_id,
            parent_close_policy=parent_close_policy,
            cancellation_type=cancellation_type,
            execution_timeout=execution_timeout,
            retry_policy=retry_policy,
            id_reuse_policy=CHILD_ID_REUSE_POLICY,
        )
        return handle, True
    except WorkflowAlreadyStartedError:
        attached = workflow.get_external_workflow_handle_for(
            TicketWorkflow.run, child_id
        )
        return attached, False


async def cancel_ticket_handle(handle: TicketChildHandle) -> None:
    """Cancel a child or attached external ticket handle."""

    result = handle.cancel()
    if hasattr(result, "__await__"):
        await result


def handle_is_done(handle: TicketChildHandle) -> bool:
    """Return True when a child handle has already finished.

    External handles have no ``done()``; they are treated as still cancellable.
    """

    done = getattr(handle, "done", None)
    return bool(done()) if callable(done) else False
