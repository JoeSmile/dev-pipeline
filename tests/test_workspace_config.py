"""Unit tests for optional workspace.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.errors import WorkspaceConfigError
from dev_pipeline.workspace_config import (
    load_authorized_root_from_path,
    load_knowledge_root_from_path,
    parse_authorized_root,
    resolve_knowledge_root,
)


def test_missing_file_is_unauthorized(tmp_path: Path) -> None:
    assert load_authorized_root_from_path(str(tmp_path)) == ""


def test_missing_config_root_is_unauthorized() -> None:
    assert load_authorized_root_from_path(None) == ""
    assert load_authorized_root_from_path("   ") == ""


def test_valid_file_returns_absolute_root(tmp_path: Path) -> None:
    root = tmp_path / "authorized"
    root.mkdir()
    (tmp_path / "workspace.toml").write_text(
        f"authorized_root = {str(root)!r}\n",
        encoding="utf-8",
    )
    assert load_authorized_root_from_path(str(tmp_path)) == str(root)


def test_relative_root_is_rejected() -> None:
    with pytest.raises(WorkspaceConfigError, match="绝对路径"):
        parse_authorized_root({"authorized_root": "relative/path"})


def test_missing_directory_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    with pytest.raises(WorkspaceConfigError, match="不存在"):
        parse_authorized_root({"authorized_root": str(missing)})


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(WorkspaceConfigError, match="未知字段"):
        parse_authorized_root({"authorized_root": "/tmp", "extra": True})


def test_unparsable_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "workspace.toml").write_text("this is not toml {", encoding="utf-8")
    with pytest.raises(WorkspaceConfigError, match="无法解析"):
        load_authorized_root_from_path(str(tmp_path))


def test_knowledge_root_is_used_when_present(tmp_path: Path) -> None:
    authorized = tmp_path / "authorized"
    knowledge = tmp_path / "knowledge"
    authorized.mkdir()
    knowledge.mkdir()
    (tmp_path / "workspace.toml").write_text(
        f"authorized_root = {str(authorized)!r}\nknowledge_root = {str(knowledge)!r}\n",
        encoding="utf-8",
    )
    assert load_authorized_root_from_path(str(tmp_path)) == str(authorized)
    assert load_knowledge_root_from_path(str(tmp_path)) == str(knowledge)
    assert resolve_knowledge_root(str(tmp_path)) == str(knowledge)


def test_knowledge_root_falls_back_to_authorized_then_config(tmp_path: Path) -> None:
    assert resolve_knowledge_root(str(tmp_path)) == str(tmp_path)
    authorized = tmp_path / "authorized"
    authorized.mkdir()
    (tmp_path / "workspace.toml").write_text(
        f"authorized_root = {str(authorized)!r}\n",
        encoding="utf-8",
    )
    assert resolve_knowledge_root(str(tmp_path)) == str(authorized)
