"""Workspace / side-effect claim activity. Does not talk to git or GitHub."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.models import TicketWorkInput, WorkspaceClaim
from dev_pipeline.workspace import claim_workspace

logger = structlog.get_logger("dev_pipeline.activities.workspace")


@activity.defn
async def claim_ticket_workspace(payload: TicketWorkInput) -> WorkspaceClaim:
    """Claim a workspace identity for this ticket execution, or reuse the first.

    A second call with the same ``ticket_id + execution_id`` returns the first
    claim and does not increment the store's created counter.

    Args:
        payload: Ticket work input carrying the idempotency key.

    Returns:
        The claimed workspace / branch / PR-like identity.
    """

    claim = claim_workspace(payload)
    logger.info(
        "workspace claim",
        ticket_id=payload.ticket_id,
        execution_id=payload.execution_id,
        workspace_id=claim.workspace_id,
        branch_name=claim.branch_name,
        pr_token=claim.pr_token,
    )
    return claim
