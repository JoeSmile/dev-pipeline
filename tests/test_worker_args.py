"""Worker CLI requires --config-root and does not invent a default path."""

from __future__ import annotations

import pytest

from dev_pipeline.constants import DEFAULT_TEMPORAL_TARGET
from dev_pipeline.worker import parse_worker_args


def test_missing_config_root_flag_errors_in_chinese() -> None:
    with pytest.raises(SystemExit):
        parse_worker_args([])


def test_empty_config_root_is_rejected() -> None:
    with pytest.raises(SystemExit):
        parse_worker_args(["--config-root", ""])


def test_config_root_and_default_temporal_target() -> None:
    args = parse_worker_args(["--config-root", r"D:\pipeline-config"])
    assert args.config_root == r"D:\pipeline-config"
    assert args.temporal_target == DEFAULT_TEMPORAL_TARGET
    custom = parse_worker_args(
        ["--config-root", "/abs/config", "--temporal-target", "localhost:8233"]
    )
    assert custom.temporal_target == "localhost:8233"
