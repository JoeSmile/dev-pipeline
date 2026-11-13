"""In-memory workspace / side-effect claims keyed by ticket_id + execution_id.

The authorized worktree path is recorded here; guarded writes live in the
coding sandbox (DP-20). Abort recycles this execution's unmerged worktrees
and local branches (DP-30) without closing or merging existing remote PRs.
Claims never point at a shared mainline branch:
every identity includes ``execution_id``.
"""

from __future__ import annotations

from dev_pipeline.models import TicketWorkInput, WorkspaceClaim, ticket_workflow_id

MAINLINE_BRANCH = "main"

_DEFAULT_STORE: WorkspaceClaimStore | None = None


def claim_key(ticket_id: str, execution_id: str) -> str:
    """Return the idempotency key used for claims and child workflow ids."""

    return ticket_workflow_id(ticket_id, execution_id)


def derive_workspace_id(ticket_id: str, execution_id: str) -> str:
    """Return a workspace identity path that includes the execution id."""

    return f"ws/{ticket_id}/{execution_id}"


def derive_branch_name(ticket_id: str, execution_id: str) -> str:
    """Return a branch name derived from the ticket execution key."""

    return f"ticket/{ticket_id}/{execution_id}"


def derive_pr_token(ticket_id: str, execution_id: str) -> str:
    """Return a PR-like token derived from the ticket execution key."""

    return f"pr-{ticket_id}-{execution_id}"


def build_claim(
    ticket_id: str,
    execution_id: str,
    *,
    authorized_worktree: str = "",
) -> WorkspaceClaim:
    """Build a claim record without writing it to a store."""

    return WorkspaceClaim(
        ticket_id=ticket_id,
        execution_id=execution_id,
        workspace_id=derive_workspace_id(ticket_id, execution_id),
        branch_name=derive_branch_name(ticket_id, execution_id),
        pr_token=derive_pr_token(ticket_id, execution_id),
        authorized_worktree=authorized_worktree,
    )


class WorkspaceClaimStore:
    """Process-local claim registry. Writes only go to the caller's key."""

    def __init__(self) -> None:
        self._claims: dict[str, WorkspaceClaim] = {}
        self._created_counts: dict[str, int] = {}

    def clear(self) -> None:
        """Drop every recorded claim. Intended for tests."""

        self._claims.clear()
        self._created_counts.clear()

    def get(self, ticket_id: str, execution_id: str) -> WorkspaceClaim | None:
        """Return the stored claim for this key, if any."""

        return self._claims.get(claim_key(ticket_id, execution_id))

    def created_count(self, ticket_id: str, execution_id: str) -> int:
        """Return how many times a new claim was created for this key (0 or 1)."""

        return self._created_counts.get(claim_key(ticket_id, execution_id), 0)

    def total_created(self) -> int:
        """Return the number of first-time claims across all keys."""

        return sum(self._created_counts.values())

    def claim(
        self,
        ticket_id: str,
        execution_id: str,
        *,
        authorized_worktree: str = "",
    ) -> WorkspaceClaim:
        """Return the existing claim for the key, or record the first one.

        A second call with the same key returns the first claim and does not
        increment the created counter. The record is fully built before it is
        stored, and only that key is written, so a failure while claiming
        ticket A cannot mutate ticket B's slot or a mainline identity.
        A later call cannot replace the first authorized worktree path.

        Args:
            ticket_id: Implementation ticket id.
            execution_id: Parent / child execution id.
            authorized_worktree: Absolute path the sandbox may write, or empty.

        Returns:
            The first claim recorded for this key.
        """

        key = claim_key(ticket_id, execution_id)
        existing = self._claims.get(key)
        if existing is not None:
            return existing
        record = build_claim(
            ticket_id,
            execution_id,
            authorized_worktree=authorized_worktree,
        )
        self._commit(key, record)
        return record

    def _commit(self, key: str, record: WorkspaceClaim) -> None:
        """Store a new claim under exactly one key."""

        self._claims[key] = record
        self._created_counts[key] = 1


def default_claim_store() -> WorkspaceClaimStore:
    """Return the process-wide default store used by production stub activities."""

    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = WorkspaceClaimStore()
    return _DEFAULT_STORE


def reset_default_claim_store() -> None:
    """Clear the process-wide default store. Intended for tests."""

    default_claim_store().clear()


def claim_workspace(
    payload: TicketWorkInput,
    *,
    store: WorkspaceClaimStore | None = None,
    authorized_worktree: str = "",
) -> WorkspaceClaim:
    """Claim or reuse the workspace identity for this ticket execution.

    Args:
        payload: Ticket work input carrying ticket_id and execution_id.
        store: Optional store; defaults to the process-wide registry.
        authorized_worktree: Absolute path the sandbox may write, or empty.

    Returns:
        The first claim for this key. Repeated calls return the same record.
    """

    target = store if store is not None else default_claim_store()
    return target.claim(
        payload.ticket_id,
        payload.execution_id,
        authorized_worktree=authorized_worktree,
    )
