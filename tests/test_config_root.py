"""Config-root must be an existing absolute directory; no silent fallback."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.config_root import validate_config_root_path
from dev_pipeline.errors import ConfigRootError


def test_missing_config_root_fails_visibly() -> None:
    with pytest.raises(ConfigRootError, match="必填参数") as exc:
        validate_config_root_path(None)
    assert "不会回退" in str(exc.value)
    with pytest.raises(ConfigRootError, match="必填参数"):
        validate_config_root_path("")
    with pytest.raises(ConfigRootError, match="必填参数"):
        validate_config_root_path("   ")


def test_relative_path_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    with pytest.raises(ConfigRootError, match="必须是绝对路径"):
        validate_config_root_path("config")
    with pytest.raises(ConfigRootError, match="必须是绝对路径"):
        validate_config_root_path("./config")


def test_nonexistent_absolute_path_is_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(ConfigRootError, match="不存在") as exc:
        validate_config_root_path(str(missing))
    assert "不会当作" in str(exc.value)


def test_file_instead_of_directory_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "config-file"
    target.write_text("not a dir", encoding="utf-8")
    with pytest.raises(ConfigRootError, match="不是目录"):
        validate_config_root_path(str(target))


def test_existing_absolute_directory_is_accepted(tmp_config_root: Path) -> None:
    assert validate_config_root_path(str(tmp_config_root)) == str(tmp_config_root)
