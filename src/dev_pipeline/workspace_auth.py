"""Resolve an authorized worktree from config and the existing claim store.

This module performs filesystem I/O. Workflow modules must not import it.
"""

from __future__ import annotations

import re
from pathlib import Path

from dev_pipeline.errors import SandboxViolation, WorkspaceConfigError
from dev_pipeline.models import TicketWorkInput, WorkspaceClaim
from dev_pipeline.sandbox import is_within_authorized
from dev_pipeline.workspace import WorkspaceClaimStore, claim_workspace, default_claim_store
from dev_pipeline.workspace_config import load_authorized_root_from_path

_UNSAFE_SEGMENT = "工作区声明标识不能包含路径分隔、盘符或 '..'，已拒绝: {value}"
_SAFE = re.compile(r"^[A-Za-z0-9._][A-Za-z0-9._-]*$")


def _require_safe_segment(value: str, *, label: str) -> str:
    text = value.strip()
    if not text or not _SAFE.fullmatch(text) or text in {".", ".."}:
        raise WorkspaceConfigError(_UNSAFE_SEGMENT.format(value=f"{label}={value!r}"))
    return text


def derive_authorized_worktree(authorized_root: str, ticket_id: str, execution_id: str) -> str:
    """Return ``<authorized_root>/<ticket_id>/<execution_id>`` after safety checks."""

    ticket = _require_safe_segment(ticket_id, label="ticket_id")
    execution = _require_safe_segment(execution_id, label="execution_id")
    return str(Path(authorized_root) / ticket / execution)


def ensure_authorized_worktree(authorized_root: str, worktree: str) -> None:
    """Create the claimed worktree directory only when it stays inside the root."""

    if not is_within_authorized(worktree, authorized_root):
        raise SandboxViolation(
            f"声明的 worktree 越出授权根目录，已拒绝: {worktree}",
            path=worktree,
            authorized_worktree=authorized_root,
        )
    Path(worktree).mkdir(parents=True, exist_ok=True)
    resolved_tree = str(Path(worktree).resolve())
    resolved_root = str(Path(authorized_root).resolve())
    if not is_within_authorized(resolved_tree, resolved_root):
        raise SandboxViolation(
            f"声明的 worktree 经解析后越出授权根目录，已拒绝: {worktree}",
            path=worktree,
            authorized_worktree=authorized_root,
        )


def resolve_workspace_claim(
    payload: TicketWorkInput,
    *,
    store: WorkspaceClaimStore | None = None,
) -> WorkspaceClaim:
    """Reuse the first claim for this key, or record an authorized worktree path.

    A missing ``workspace.toml`` leaves ``authorized_worktree`` empty. The same
    key never creates a second worktree identity. A new ``execution_id`` gets a
    new path under the authorized root.
    """

    target = store if store is not None else default_claim_store()
    existing = target.get(payload.ticket_id, payload.execution_id)
    if existing is not None:
        return existing
    root = load_authorized_root_from_path(payload.config_root)
    worktree = ""
    if root:
        worktree = derive_authorized_worktree(root, payload.ticket_id, payload.execution_id)
        ensure_authorized_worktree(root, worktree)
    return claim_workspace(payload, store=target, authorized_worktree=worktree)
