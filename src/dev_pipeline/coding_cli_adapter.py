"""Reserved Cursor CLI adapter for later DP-46 wiring.

This module only plans the legal coding argv. It does not start a process,
does not read ``CURSOR_API_KEY``, and is not called from parent or child
workflows or from the DP-21 deliverable backend. Workflow modules must not
import this file.
"""

from __future__ import annotations

from dataclasses import dataclass

from dev_pipeline.coding_cli import assert_coding_argv_legal, build_coding_argv


@dataclass(frozen=True)
class CodingCliPlan:
    """A reserved, unwired coding CLI invocation plan. Contains no secrets."""

    argv: list[str]
    authorized_worktree: str
    wired: bool = False
    invoked: bool = False


def plan_coding_cli(authorized_worktree: str) -> CodingCliPlan:
    """Build the DP-46 coding argv and leave it unwired.

    Args:
        authorized_worktree: The pipeline-authorized directory used as
            ``--workspace``. Must not be empty.

    Returns:
        A plan whose ``wired`` and ``invoked`` flags stay False. The argv
        never includes ``--worktree`` or ``--api-key``.
    """

    argv = build_coding_argv(authorized_worktree)
    assert_coding_argv_legal(argv, authorized_worktree)
    return CodingCliPlan(
        argv=list(argv),
        authorized_worktree=authorized_worktree,
        wired=False,
        invoked=False,
    )
