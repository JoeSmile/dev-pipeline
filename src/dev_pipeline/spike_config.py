"""Load optional spike.toml from the config-root. This module performs I/O.

Workflow modules must not import this file. Call it from the
``load_spike_config`` activity only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from dev_pipeline.errors import SpikeConfigError
from dev_pipeline.models import DEFAULT_SPIKE_TIMEOUT_SECONDS, SpikeConfig

SPIKE_FILENAME = "spike.toml"
_ALLOWED_TOP_LEVEL = frozenset({"timeout_seconds"})
_UNREADABLE = "Spike 配置无法读取: {path}（{exc}）"
_UNPARSABLE = "Spike 配置无法解析: {path}（{exc}）"
_UNKNOWN_KEY = "Spike 配置包含未知字段: {key}。"
_BAD_TIMEOUT = "Spike 时限 timeout_seconds 必须是大于 0 的整数，已拒绝: {value}"
_NOT_TABLE = "Spike 配置必须是表。"


def load_spike_config_from_path(config_root: str) -> SpikeConfig:
    """Return spike duration from ``<config_root>/spike.toml``, or the 待证 default.

    A missing file is not an error: the 待证 placeholder timeout is used.
    A present but invalid file fails visibly and is never replaced by a
    hidden official SLA.
    """

    path = Path(config_root) / SPIKE_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise SpikeConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        return SpikeConfig(
            timeout_seconds=DEFAULT_SPIKE_TIMEOUT_SECONDS,
            source="待证默认",
        )
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SpikeConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise SpikeConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    if not isinstance(data, dict):
        raise SpikeConfigError(_NOT_TABLE)
    return parse_spike_config(data, source=str(path))


def parse_spike_config(data: dict[str, Any], *, source: str) -> SpikeConfig:
    """Validate a parsed spike table and return a SpikeConfig."""

    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise SpikeConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    if "timeout_seconds" not in data:
        return SpikeConfig(
            timeout_seconds=DEFAULT_SPIKE_TIMEOUT_SECONDS,
            source=source,
        )
    value = data["timeout_seconds"]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise SpikeConfigError(_BAD_TIMEOUT.format(value=value))
    return SpikeConfig(timeout_seconds=value, source=source)
