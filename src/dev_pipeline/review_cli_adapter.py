"""Reserved Cursor CLI adapter for dual Review, aligned with DP-46 flags.

This module only plans the legal review argv and records a pre-run workspace
fingerprint. It does not start a process, does not read ``CURSOR_API_KEY``,
and is not called from parent or child workflows or from the deterministic
review backend. Workflow modules must not import this file.
"""

from __future__ import annotations

from dataclasses import dataclass

from dev_pipeline.review_cli import (
    assert_review_argv_legal,
    assert_workspace_unchanged,
    build_review_argv,
    workspace_fingerprint,
)


@dataclass(frozen=True)
class ReviewCliPlan:
    """A reserved, unwired review CLI invocation plan. Contains no secrets."""

    argv: list[str]
    authorized_worktree: str
    fingerprint_before: str
    wired: bool = False
    invoked: bool = False


def plan_review_cli(authorized_worktree: str) -> ReviewCliPlan:
    """Build the DP-46 review argv and leave it unwired.

    The argv is ``agent -p --mode=ask --workspace <授权目录>``. It never
    includes ``--force``, ``--resume``, ``--continue``, ``--worktree``, or
    ``--api-key``. After a reserved run, callers must check the workspace
    is unchanged; this planner does not invoke the CLI.

    Args:
        authorized_worktree: The pipeline-authorized directory used as
            ``--workspace``. Must not be empty.

    Returns:
        A plan whose ``wired`` and ``invoked`` flags stay False.
    """
    argv = build_review_argv(authorized_worktree)
    assert_review_argv_legal(argv, authorized_worktree)
    return ReviewCliPlan(
        argv=list(argv),
        authorized_worktree=authorized_worktree,
        fingerprint_before=workspace_fingerprint(authorized_worktree),
        wired=False,
        invoked=False,
    )


def confirm_reserved_review_unchanged(plan: ReviewCliPlan) -> None:
    """Verify the worktree still matches the fingerprint taken at plan time.

    Because this adapter never invokes the CLI, a clean tree stays unchanged.
    """
    if plan.invoked or plan.wired:
        raise RuntimeError("预留审查适配器不得接线或调用现场 CLI")
    assert_workspace_unchanged(plan.authorized_worktree, plan.fingerprint_before)
