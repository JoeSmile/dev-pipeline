"""Workspace / side-effect claim activity. Resolves the authorized worktree."""

from __future__ import annotations

import structlog
from temporalio import activity

from dev_pipeline.models import TicketWorkInput, WorkspaceClaim
from dev_pipeline.workspace_auth import resolve_workspace_claim

logger = structlog.get_logger("dev_pipeline.activities.workspace")


@activity.defn
async def claim_ticket_workspace(payload: TicketWorkInput) -> WorkspaceClaim:
    """Claim a workspace identity for this ticket execution, or reuse the first.

    A second call with the same ``ticket_id + execution_id`` returns the first
    claim and does not increment the store's created counter. A configured
    authorized root produces one worktree path under that root; a missing
    ``workspace.toml`` leaves the claim unauthorized.

    Args:
        payload: Ticket work input carrying the idempotency key and config-root.

    Returns:
        The claimed workspace / branch / authorized worktree identity.
    """

    claim = resolve_workspace_claim(payload)
    logger.info(
        "workspace claim",
        ticket_id=payload.ticket_id,
        execution_id=payload.execution_id,
        workspace_id=claim.workspace_id,
        branch_name=claim.branch_name,
        pr_token=claim.pr_token,
        authorized=bool(claim.authorized_worktree),
    )
    return claim
