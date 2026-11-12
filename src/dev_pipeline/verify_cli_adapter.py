"""Reserved Cursor CLI adapter for a later verifying path.

This module only records that a live CLI verify plan is not wired. It
does not start a process, does not read ``CURSOR_API_KEY``, and is not
called from parent or child workflows or from the DP-24 verify backend.
Workflow modules must not import this file.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class VerifyCliPlan:
    """A reserved, unwired verifying CLI invocation plan. Contains no secrets."""

    authorized_worktree: str
    wired: bool = False
    invoked: bool = False


def plan_verify_cli(authorized_worktree: str) -> VerifyCliPlan:
    """Return an unwired verifying CLI plan for a later ticket.

    Args:
        authorized_worktree: The pipeline-authorized directory. Must not be empty.

    Returns:
        A plan whose ``wired`` and ``invoked`` flags stay False.
    """

    return VerifyCliPlan(
        authorized_worktree=authorized_worktree,
        wired=False,
        invoked=False,
    )
