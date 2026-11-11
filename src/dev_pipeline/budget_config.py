"""Load and validate budget.toml from the config-root. This module performs I/O.

Workflow modules must not import this file. Call it from the worker entrypoint
and from the load_budget_config activity only.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from dev_pipeline.budget import BUDGET_UNITS, BudgetAmounts, BudgetConfig
from dev_pipeline.errors import BudgetConfigError

BUDGET_FILENAME = "budget.toml"
_ALLOWED_TOP_LEVEL = frozenset({"units", "limits"})
_MISSING = "配置根目录缺少预算配置文件 budget.toml: {path}。不会使用隐藏默认限额继续执行。"
_UNREADABLE = "预算配置无法读取: {path}（{exc}）"
_UNPARSABLE = "预算配置无法解析: {path}（{exc}）"
_NEED_UNITS = "预算配置必须至少启用一个计量单位（tokens / calls / cost）。"
_UNKNOWN_UNIT = "预算配置包含未知计量单位: {unit}。合法单位为 tokens / calls / cost。"
_UNKNOWN_KEY = "预算配置包含未知字段: {key}。"
_UNKNOWN_LIMIT = "预算配置包含未知限额字段: {key}。合法字段为 tokens / calls / cost。"
_MISSING_LIMIT = "已启用计量单位 {unit}，但未配置限额。"
_BAD_LIMIT = "预算限额 {unit} 必须是大于或等于 0 的整数，已拒绝: {value}"
_LIMITS_NOT_TABLE = "预算配置的 limits 必须是表。"
_UNITS_NOT_ARRAY = "预算配置的 units 必须是字符串数组。"


def load_budget_config_from_path(config_root: str) -> BudgetConfig:
    """Return the budget config from ``<config_root>/budget.toml``, or fail visibly.

    A missing or invalid file is never treated as optional, and no hidden
    numeric quota is substituted.
    """

    path = Path(config_root) / BUDGET_FILENAME
    try:
        exists = path.is_file()
    except OSError as exc:
        raise BudgetConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    if not exists:
        raise BudgetConfigError(_MISSING.format(path=path))
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise BudgetConfigError(_UNREADABLE.format(path=path, exc=exc)) from exc
    try:
        data = tomllib.loads(raw)
    except tomllib.TOMLDecodeError as exc:
        raise BudgetConfigError(_UNPARSABLE.format(path=path, exc=exc)) from exc
    return parse_budget_config(data)


def parse_budget_config(data: dict[str, Any]) -> BudgetConfig:
    """Validate a parsed budget table and return a BudgetConfig."""

    unknown = sorted(set(data) - _ALLOWED_TOP_LEVEL)
    if unknown:
        raise BudgetConfigError(_UNKNOWN_KEY.format(key=", ".join(unknown)))
    units = _parse_units(data.get("units"))
    limits_raw = data.get("limits", {})
    if not isinstance(limits_raw, dict):
        raise BudgetConfigError(_LIMITS_NOT_TABLE)
    unknown_limits = sorted(set(limits_raw) - set(BUDGET_UNITS))
    if unknown_limits:
        raise BudgetConfigError(_UNKNOWN_LIMIT.format(key=", ".join(unknown_limits)))
    amounts: dict[str, int] = {
        "tokens": 0,
        "calls": 0,
        "cost": 0,
    }
    for unit in BUDGET_UNITS:
        if unit in limits_raw:
            amounts[unit] = _parse_limit(unit, limits_raw[unit])
    for enabled in units:
        if enabled not in limits_raw:
            raise BudgetConfigError(_MISSING_LIMIT.format(unit=enabled))
    return BudgetConfig(
        units=units,
        limits=BudgetAmounts(
            tokens=amounts["tokens"],
            calls=amounts["calls"],
            cost=amounts["cost"],
        ),
    )


def _parse_units(raw: object) -> list[str]:
    if raw is None:
        raise BudgetConfigError(_NEED_UNITS)
    if not isinstance(raw, list) or any(not isinstance(item, str) for item in raw):
        raise BudgetConfigError(_UNITS_NOT_ARRAY)
    units = [item.strip() for item in raw]
    if not units or any(not item for item in units):
        raise BudgetConfigError(_NEED_UNITS)
    for unit in units:
        if unit not in BUDGET_UNITS:
            raise BudgetConfigError(_UNKNOWN_UNIT.format(unit=unit))
    # Preserve declaration order, drop duplicates.
    unique: list[str] = []
    for unit in units:
        if unit not in unique:
            unique.append(unit)
    return unique


def _parse_limit(unit: str, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BudgetConfigError(_BAD_LIMIT.format(unit=unit, value=raw))
    if raw < 0:
        raise BudgetConfigError(_BAD_LIMIT.format(unit=unit, value=raw))
    return raw
