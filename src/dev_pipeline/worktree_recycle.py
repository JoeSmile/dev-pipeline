"""DP-30 hook for reclaiming unmerged worktrees created by one execution.

This ticket only records the intent. It does not delete directories, run
``git worktree``, or touch real project trees. Workflow modules must not
import this file.
"""

from __future__ import annotations

from dev_pipeline.models import PENDING_FIELD

RECYCLE_PENDING = PENDING_FIELD
_HOOK_DETAIL = "Child 结束或 abort 后，本 execution 创建的未合并 worktree 按 DP-30 回收（待证）"


def worktree_recycle_hook(worktree: str, *, ticket_id: str, execution_id: str) -> str:
    """Return the 待证 recycle marker. Does not remove or leak a worktree.

    Args:
        worktree: Authorized worktree path created for this execution.
        ticket_id: Implementation ticket id.
        execution_id: Execution id that created the tree.

    Returns:
        The pending marker ``待证``. No filesystem mutation is performed.
    """

    del worktree, ticket_id, execution_id
    return RECYCLE_PENDING


def recycle_hook_detail() -> str:
    """Return the operator-visible explanation of the pending recycle hook."""

    return _HOOK_DETAIL
