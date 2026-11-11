"""Unit tests for the deterministic parent budget ledger (no Temporal I/O)."""

from __future__ import annotations

from dev_pipeline.budget import (
    COSTLY_PATHS,
    BudgetAmounts,
    BudgetConfig,
    add_usage,
    build_budget_snapshot,
    decide_start,
    exhausted_unit,
    remaining_amounts,
    remaining_of,
)


def _config(*units: str, tokens: int = 100, calls: int = 10, cost: int = 20) -> BudgetConfig:
    limits = BudgetAmounts(tokens=tokens, calls=calls, cost=cost)
    return BudgetConfig(units=list(units), limits=limits)


def test_remaining_and_exhausted_use_only_enabled_units() -> None:
    config = _config("tokens", tokens=10)
    spent = BudgetAmounts(tokens=7, calls=99, cost=99)
    assert remaining_of(config.limits, spent, "tokens") == 3
    assert exhausted_unit(config, spent) is None
    assert exhausted_unit(config, BudgetAmounts(tokens=10)) == "tokens"
    # Disabled units may already look empty, but they do not trip the gate.
    assert exhausted_unit(config, BudgetAmounts(tokens=0, calls=10, cost=20)) is None


def test_remaining_amounts_are_clamped_at_zero() -> None:
    config = _config("calls", calls=2)
    remaining = remaining_amounts(config, BudgetAmounts(calls=5))
    assert remaining.calls == 0
    assert remaining.tokens == 0
    assert remaining.cost == 0


def test_add_usage_accumulates_all_units() -> None:
    spent = add_usage(
        BudgetAmounts(tokens=1, calls=1, cost=2),
        BudgetAmounts(tokens=4, calls=2, cost=3),
    )
    assert spent == BudgetAmounts(tokens=5, calls=3, cost=5)


def test_gate_allows_start_when_budget_remains() -> None:
    decision = decide_start(
        _config("calls", calls=2), BudgetAmounts(), stopped_paths=(), path="rag"
    )
    assert decision.allowed is True
    assert decision.reason == ""


def test_gate_refuses_start_when_any_enabled_unit_is_exhausted() -> None:
    decision = decide_start(
        _config("tokens", "calls", tokens=8, calls=1),
        BudgetAmounts(tokens=3, calls=1),
        stopped_paths=(),
        path="model",
    )
    assert decision.allowed is False
    assert decision.unit == "calls"
    assert "Bad" in decision.reason
    assert "calls" in decision.reason
    assert "待证" in decision.reason
    assert "model" in decision.reason


def test_exhausted_budget_reason_outweighs_already_stopped() -> None:
    decision = decide_start(
        _config("calls", calls=1),
        BudgetAmounts(calls=1),
        stopped_paths=COSTLY_PATHS,
        path="model",
    )
    assert decision.allowed is False
    assert decision.unit == "calls"
    assert "Bad" in decision.reason


def test_gate_refuses_a_path_already_stopped_for_no_new_evidence() -> None:
    decision = decide_start(
        _config("calls", calls=10),
        BudgetAmounts(),
        stopped_paths=("debate",),
        path="debate",
    )
    assert decision.allowed is False
    assert "无新证据" in decision.reason or "已停止" in decision.reason
    # Sibling costly paths remain available while budget remains.
    sibling = decide_start(
        _config("calls", calls=10),
        BudgetAmounts(),
        stopped_paths=("debate",),
        path="rag",
    )
    assert sibling.allowed is True


def test_breach_makes_every_costly_path_unavailable() -> None:
    config = _config("cost", cost=1)
    spent = BudgetAmounts(cost=1)
    for path in COSTLY_PATHS:
        decision = decide_start(config, spent, stopped_paths=COSTLY_PATHS, path=path)
        assert decision.allowed is False, path


def test_limits_come_from_config_not_a_hard_coded_quota() -> None:
    tight = decide_start(_config("calls", calls=0), BudgetAmounts(), stopped_paths=(), path="rag")
    loose = decide_start(_config("calls", calls=3), BudgetAmounts(), stopped_paths=(), path="rag")
    assert tight.allowed is False
    assert loose.allowed is True
    other = decide_start(
        _config("tokens", tokens=1),
        BudgetAmounts(tokens=1),
        stopped_paths=(),
        path="debate",
    )
    assert other.allowed is False
    assert other.unit == "tokens"


def test_snapshot_exposes_spent_limits_remaining_and_breach() -> None:
    config = _config("tokens", "calls", tokens=10, calls=4)
    spent = BudgetAmounts(tokens=7, calls=4)
    snap = build_budget_snapshot(
        config,
        spent,
        stopped_paths=("debate", "rag", "model"),
        no_new_evidence_paths=(),
        last_breach_path="rag",
        last_breach_unit="calls",
    )
    assert snap.limits == config.limits
    assert snap.spent == spent
    assert snap.remaining == BudgetAmounts(tokens=3, calls=0, cost=0)
    assert snap.units == ["tokens", "calls"]
    assert snap.last_breach is not None
    assert snap.last_breach.bad == "Bad"
    assert snap.last_breach.path == "rag"
    assert snap.last_breach.unit == "calls"
    assert "待证" in snap.last_breach.reason
    assert set(snap.stopped_paths) == set(COSTLY_PATHS)


def test_invalid_or_empty_path_is_refused() -> None:
    decision = decide_start(
        _config("calls", calls=5), BudgetAmounts(), stopped_paths=(), path="spike"
    )
    assert decision.allowed is False
    assert "未知" in decision.reason or "非法" in decision.reason
