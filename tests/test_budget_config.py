"""budget.toml must be present and valid; missing files do not fall back to a hidden quota."""

from __future__ import annotations

from pathlib import Path

import pytest

from dev_pipeline.budget_config import load_budget_config_from_path, parse_budget_config
from dev_pipeline.errors import BudgetConfigError


def _write(root: Path, text: str) -> Path:
    path = root / "budget.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_missing_budget_file_fails_visibly_in_chinese(tmp_path: Path) -> None:
    with pytest.raises(BudgetConfigError, match="缺少预算配置文件") as exc:
        load_budget_config_from_path(str(tmp_path))
    message = str(exc.value)
    assert "budget.toml" in message
    assert "不会使用隐藏默认限额" in message


def test_invalid_toml_fails_visibly(tmp_path: Path) -> None:
    _write(tmp_path, "units = [")
    with pytest.raises(BudgetConfigError, match="无法解析") as exc:
        load_budget_config_from_path(str(tmp_path))
    assert "budget.toml" in str(exc.value)


def test_at_least_one_unit_is_required() -> None:
    with pytest.raises(BudgetConfigError, match="至少启用一个计量单位"):
        parse_budget_config({"units": [], "limits": {"tokens": 1}})
    with pytest.raises(BudgetConfigError, match="至少启用一个计量单位"):
        parse_budget_config({"limits": {"tokens": 1}})


def test_unknown_unit_is_rejected() -> None:
    with pytest.raises(BudgetConfigError, match="未知计量单位"):
        parse_budget_config({"units": ["tokens", "gpu-hours"], "limits": {"tokens": 1}})


def test_enabled_unit_without_limit_is_rejected() -> None:
    with pytest.raises(BudgetConfigError, match="未配置限额"):
        parse_budget_config({"units": ["cost"], "limits": {"tokens": 10}})


def test_negative_or_non_integer_limit_is_rejected() -> None:
    with pytest.raises(BudgetConfigError, match="大于或等于 0 的整数"):
        parse_budget_config({"units": ["calls"], "limits": {"calls": -1}})
    with pytest.raises(BudgetConfigError, match="大于或等于 0 的整数"):
        parse_budget_config({"units": ["calls"], "limits": {"calls": 1.5}})


def test_valid_file_loads_configured_limits(tmp_path: Path) -> None:
    _write(
        tmp_path,
        """
# 限额数字均为待证占位，须经负责人签批后才视为正式限额。
units = ["tokens", "calls"]

[limits]
tokens = 42
calls = 7
""",
    )
    config = load_budget_config_from_path(str(tmp_path))
    assert config.units == ["tokens", "calls"]
    assert config.limits.tokens == 42
    assert config.limits.calls == 7
    assert config.limits.cost == 0


def test_different_files_yield_different_limits(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    _write(first, 'units = ["calls"]\n[limits]\ncalls = 1\n')
    _write(second, 'units = ["tokens"]\n[limits]\ntokens = 9\n')
    assert load_budget_config_from_path(str(first)).limits.calls == 1
    assert load_budget_config_from_path(str(second)).limits.tokens == 9


def test_unknown_top_level_or_limit_key_is_rejected() -> None:
    with pytest.raises(BudgetConfigError, match="未知"):
        parse_budget_config({"units": ["calls"], "limits": {"calls": 1}, "secret": "nope"})
    with pytest.raises(BudgetConfigError, match="未知"):
        parse_budget_config({"units": ["calls"], "limits": {"calls": 1, "usd": 3}})

def test_example_budget_file_is_pending_and_loadable(tmp_path: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "docs" / "examples" / "budget.toml"
    text = example.read_text(encoding="utf-8")
    assert "待证" in text
    dest = tmp_path / "budget.toml"
    dest.write_text(text, encoding="utf-8")
    config = load_budget_config_from_path(str(tmp_path))
    assert "tokens" in config.units
    assert "calls" in config.units
    assert "cost" in config.units
    # The example numbers are placeholders, not a signed-off product quota.
    assert "须经负责人签批" in text

