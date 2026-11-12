"""Unit tests for the reserved coding Cursor CLI argv / env policy."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.coding_cli import (
    assert_coding_argv_legal,
    build_coding_argv,
    build_coding_env,
    raise_for_cli_exit,
)
from dev_pipeline.errors import ConfigRootError, SandboxViolation


def test_coding_argv_uses_force_trust_and_workspace() -> None:
    authorized = r"C:\wt\T1\exec-1"
    argv = build_coding_argv(authorized)
    assert argv == [
        "agent",
        "-p",
        "--force",
        "--trust",
        "--workspace",
        authorized,
    ]
    assert "--worktree" not in argv
    assert "--api-key" not in argv
    assert_coding_argv_legal(argv, authorized)


def test_coding_argv_rejects_empty_authorization() -> None:
    with pytest.raises(SandboxViolation, match="未授权"):
        build_coding_argv("")


def test_coding_argv_rejects_worktree_and_api_key_flags() -> None:
    authorized = "/tmp/wt/T1/exec-1"
    legal = build_coding_argv(authorized)
    with pytest.raises(SandboxViolation, match="--worktree"):
        assert_coding_argv_legal([*legal, "--worktree", "extra"], authorized)
    with pytest.raises(SandboxViolation, match="--api-key"):
        assert_coding_argv_legal([*legal, "--api-key", "SECRET"], authorized)


def test_coding_env_injects_key_from_config_root_only(tmp_path: Path) -> None:
    (tmp_path / "CURSOR_API_KEY").write_text("super-secret-key\n", encoding="utf-8")
    env = build_coding_env(str(tmp_path), base_env={"PATH": "/bin"})
    argv = build_coding_argv(str(tmp_path / "T1" / "exec-1"))
    assert env["CURSOR_API_KEY"] == "super-secret-key"
    assert env["PATH"] == "/bin"
    joined = " ".join(argv)
    assert "super-secret-key" not in joined
    assert "--api-key" not in argv


def test_missing_config_root_or_key_fails_visibly(tmp_path: Path) -> None:
    with pytest.raises(ConfigRootError, match="配置根目录是必填参数"):
        build_coding_env("")
    with pytest.raises(ConfigRootError, match="CURSOR_API_KEY"):
        build_coding_env(str(tmp_path))
    (tmp_path / "CURSOR_API_KEY").write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigRootError, match="CURSOR_API_KEY"):
        build_coding_env(str(tmp_path))


def test_nonzero_cli_exit_is_not_swallowed() -> None:
    raise_for_cli_exit(0)
    with pytest.raises(SandboxViolation, match="非 0"):
        raise_for_cli_exit(2)


def test_key_is_not_written_into_evidence_preview(tmp_path: Path) -> None:
    (tmp_path / "CURSOR_API_KEY").write_text("super-secret-key", encoding="utf-8")
    env = build_coding_env(str(tmp_path))
    argv = build_coding_argv(str(tmp_path / "work"))
    preview = "cmd=" + " ".join(argv)
    assert env["CURSOR_API_KEY"] not in preview
