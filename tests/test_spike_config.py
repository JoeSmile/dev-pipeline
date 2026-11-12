"""Unit tests for optional spike.toml loading."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.errors import SpikeConfigError
from dev_pipeline.models import DEFAULT_SPIKE_TIMEOUT_SECONDS
from dev_pipeline.spike_config import load_spike_config_from_path, parse_spike_config


def test_missing_file_uses_pending_default(tmp_path: Path) -> None:
    config = load_spike_config_from_path(str(tmp_path))
    assert config.timeout_seconds == DEFAULT_SPIKE_TIMEOUT_SECONDS
    assert config.source == "待证默认"


def test_valid_file_overrides_timeout(tmp_path: Path) -> None:
    (tmp_path / "spike.toml").write_text("timeout_seconds = 2\n", encoding="utf-8")
    config = load_spike_config_from_path(str(tmp_path))
    assert config.timeout_seconds == 2
    assert str(tmp_path / "spike.toml") in config.source


def test_invalid_timeout_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "spike.toml").write_text("timeout_seconds = 0\n", encoding="utf-8")
    with pytest.raises(SpikeConfigError, match="大于 0"):
        load_spike_config_from_path(str(tmp_path))


def test_unknown_key_is_rejected() -> None:
    with pytest.raises(SpikeConfigError, match="未知字段"):
        parse_spike_config({"timeout_seconds": 10, "cursor": True}, source="mem")


def test_unparsable_file_is_rejected(tmp_path: Path) -> None:
    (tmp_path / "spike.toml").write_text("this is not toml {", encoding="utf-8")
    with pytest.raises(SpikeConfigError, match="无法解析"):
        load_spike_config_from_path(str(tmp_path))
