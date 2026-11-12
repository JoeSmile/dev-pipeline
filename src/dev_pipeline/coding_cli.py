"""Reserved Cursor CLI argument policy for coding. This module does no live I/O
to a model, except reading ``CURSOR_API_KEY`` from the config-root when building
an env mapping.

Workflow modules must not import this file. The live Cursor CLI runtime is
DP-46; this ticket only reserves the coding argv / env contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from dev_pipeline.errors import ConfigRootError, SandboxViolation
from dev_pipeline.sandbox import is_within_authorized, remote_git_action_reason

CODING_CLI = "agent"
CURSOR_API_KEY_FILE = "CURSOR_API_KEY"
_UNAUTH = "未授权工作区，拒绝构建写码命令"
_WORKTREE_FLAG = "写码命令禁止使用 CLI 的 --worktree（会在 ~/.cursor/worktrees 另开树）"
_API_KEY_FLAG = "写码命令禁止使用 --api-key；密钥只能经配置根目录注入环境变量 CURSOR_API_KEY"
_MISSING_WORKSPACE = "写码命令缺少 --workspace 授权目录"
_WORKSPACE_MISMATCH = "写码命令的 --workspace 必须是授权 worktree，已拒绝: {path}"
_MISSING_FLAGS = "写码命令必须包含 -p、--force、--trust 和 --workspace"
_MISSING_KEY = "配置根目录缺少 CURSOR_API_KEY，拒绝构建写码命令。"
_MISSING_ROOT = "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。"
_CLI_FAILED = "Cursor CLI 以非 0 状态退出: {code}。该失败不得当作成功。"


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


def build_coding_argv(authorized_worktree: str) -> list[str]:
    """Return the only allowed coding argv: agent -p --force --trust --workspace <dir>.

    The CLI flag ``--worktree`` is never added. The API key is never placed on
    the command line.
    """

    worktree = authorized_worktree.strip()
    if not worktree:
        raise SandboxViolation(_UNAUTH)
    return [
        CODING_CLI,
        "-p",
        "--force",
        "--trust",
        "--workspace",
        worktree,
    ]


def assert_coding_argv_legal(argv: list[str], authorized_worktree: str) -> None:
    """Reject argv that leaves the authorized worktree or leaks a key onto the CLI."""

    if not authorized_worktree.strip():
        raise SandboxViolation(_UNAUTH)
    if _has_flag(argv, "--worktree"):
        raise SandboxViolation(_WORKTREE_FLAG)
    if _has_flag(argv, "--api-key"):
        raise SandboxViolation(_API_KEY_FLAG)
    git_reason = remote_git_action_reason(argv)
    if git_reason:
        raise SandboxViolation(git_reason)
    if (
        not argv
        or argv[0] != CODING_CLI
        or "-p" not in argv
        or not _has_flag(argv, "--force")
        or not _has_flag(argv, "--trust")
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


def load_cursor_api_key(config_root: str) -> str:
    """Return the CURSOR_API_KEY file contents from *config_root*, or fail visibly.

    The value is never logged here. Callers must not write it to History or
    evidence files.
    """

    if not config_root or not str(config_root).strip():
        raise ConfigRootError(_MISSING_ROOT)
    path = Path(str(config_root).strip()) / CURSOR_API_KEY_FILE
    try:
        present = path.is_file()
    except OSError as exc:
        raise ConfigRootError(f"配置根目录无法访问: {config_root}（{exc}）") from exc
    if not present:
        raise ConfigRootError(_MISSING_KEY)
    try:
        key = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ConfigRootError(f"CURSOR_API_KEY 无法读取: {path}（{exc}）") from exc
    if not key:
        raise ConfigRootError(_MISSING_KEY)
    return key


def build_coding_env(
    config_root: str,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a process env with CURSOR_API_KEY from the config-root only."""

    env = dict(base_env or {})
    env["CURSOR_API_KEY"] = load_cursor_api_key(config_root)
    return env


def raise_for_cli_exit(code: int) -> None:
    """Fail visibly on a non-zero Cursor CLI exit. Never swallow the status."""

    if code != 0:
        raise SandboxViolation(_CLI_FAILED.format(code=code))
