"""Reserved Cursor CLI argument policy for dual Review. This module does no live
I/O to a model, except reading ``CURSOR_API_KEY`` from the config-root when
building an env mapping (not used by the reserved adapter in this ticket).

Workflow modules must not import this file. The live Cursor CLI runtime is
DP-46; this ticket only reserves the review argv / workspace-unchanged contract.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path

from dev_pipeline.coding_cli import (
    CODING_CLI,
    build_coding_env,
    raise_for_cli_exit,
)
from dev_pipeline.errors import SandboxViolation
from dev_pipeline.sandbox import is_within_authorized, remote_git_action_reason

_UNAUTH = "未授权工作区，拒绝构建审查命令"
_WORKTREE_FLAG = "审查命令禁止使用 CLI 的 --worktree（会在 ~/.cursor/worktrees 另开树）"
_API_KEY_FLAG = "审查命令禁止使用 --api-key；密钥只能经配置根目录注入环境变量 CURSOR_API_KEY"
_FORCE_FLAG = "审查命令禁止使用 --force"
_RESUME_FLAG = "审查命令禁止使用 --resume / --continue；各起一次独立进程"
_MISSING_WORKSPACE = "审查命令缺少 --workspace 授权目录"
_WORKSPACE_MISMATCH = "审查命令的 --workspace 必须是授权 worktree，已拒绝: {path}"
_MISSING_FLAGS = "审查命令必须包含 -p、--mode=ask 和 --workspace"
_CHANGED = "审查结束后工作区发生改动，已拒绝该次审查写入"


def _flag_values(argv: list[str], flag: str) -> list[str]:
    values: list[str] = []
    i = 0
    while i < len(argv):
        token = argv[i]
        if token == flag:
            if i + 1 < len(argv):
                values.append(argv[i + 1])
                i += 2
                continue
            values.append("")
            i += 1
            continue
        prefix = flag + "="
        if token.startswith(prefix):
            values.append(token[len(prefix) :])
        i += 1
    return values


def _has_flag(argv: list[str], flag: str) -> bool:
    return flag in argv or any(token.startswith(flag + "=") for token in argv)


def _mode_values(argv: list[str]) -> list[str]:
    values = _flag_values(argv, "--mode")
    if "ask" in argv and "--mode" not in argv and not any(
        token.startswith("--mode=") for token in argv
    ):
        values.append("ask")
    return values


def build_review_argv(authorized_worktree: str) -> list[str]:
    """Return the only allowed review argv: agent -p --mode=ask --workspace <dir>.

    The CLI flags ``--force``, ``--resume``, ``--continue``, ``--worktree``,
    and ``--api-key`` are never added.
    """
    worktree = authorized_worktree.strip()
    if not worktree:
        raise SandboxViolation(_UNAUTH)
    return [
        CODING_CLI,
        "-p",
        "--mode=ask",
        "--workspace",
        worktree,
    ]


def assert_review_argv_legal(argv: list[str], authorized_worktree: str) -> None:
    """Reject argv that writes, resumes a session, leaves the tree, or leaks a key."""
    if not authorized_worktree.strip():
        raise SandboxViolation(_UNAUTH)
    if _has_flag(argv, "--worktree"):
        raise SandboxViolation(_WORKTREE_FLAG)
    if _has_flag(argv, "--api-key"):
        raise SandboxViolation(_API_KEY_FLAG)
    if _has_flag(argv, "--force"):
        raise SandboxViolation(_FORCE_FLAG)
    if _has_flag(argv, "--resume") or _has_flag(argv, "--continue"):
        raise SandboxViolation(_RESUME_FLAG)
    git_reason = remote_git_action_reason(argv)
    if git_reason:
        raise SandboxViolation(git_reason)
    modes = _mode_values(argv)
    if (
        not argv
        or argv[0] != CODING_CLI
        or "-p" not in argv
        or "ask" not in modes
        or not _has_flag(argv, "--workspace")
    ):
        raise SandboxViolation(_MISSING_FLAGS)
    workspaces = _flag_values(argv, "--workspace")
    if not workspaces or not workspaces[-1].strip():
        raise SandboxViolation(_MISSING_WORKSPACE)
    workspace = workspaces[-1].strip()
    if not is_within_authorized(workspace, authorized_worktree) or not is_within_authorized(
        authorized_worktree, workspace
    ):
        raise SandboxViolation(_WORKSPACE_MISMATCH.format(path=workspace))


def build_review_env(
    config_root: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a process env with CURSOR_API_KEY from the config-root only.

    The reserved adapter does not call this. Callers must not write the key
    to History or evidence files.
    """
    return build_coding_env(config_root, base_env)


def workspace_fingerprint(worktree: str) -> str:
    """Return a stable fingerprint of files in *worktree*, excluding ``.git``."""
    root = Path(worktree)
    if not worktree.strip() or not root.exists():
        return ""
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.parts
    )
    for path in paths:
        rel = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(rel)
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError:
            digest.update(b"<unreadable>")
        digest.update(b"\0")
    return digest.hexdigest()


def assert_workspace_unchanged(worktree: str, before: str) -> None:
    """Fail if the worktree changed after a reserved review run."""
    after = workspace_fingerprint(worktree)
    if after != before:
        raise SandboxViolation(_CHANGED)


__all__ = [
    "assert_review_argv_legal",
    "assert_workspace_unchanged",
    "build_review_argv",
    "build_review_env",
    "raise_for_cli_exit",
    "workspace_fingerprint",
]
