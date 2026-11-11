"""Deterministic budget ledger and snapshot types. Workflows may import this module.

This module performs no I/O and must not read the clock, draw random numbers,
or call external services. Usage figures are supplied by Activities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

CostlyPathName = Literal["debate", "rag", "model"]
BudgetUnitName = Literal["tokens", "calls", "cost"]
COSTLY_PATHS: tuple[CostlyPathName, ...] = ("debate", "rag", "model")
BUDGET_UNITS: tuple[BudgetUnitName, ...] = ("tokens", "calls", "cost")

_LIMITS_PENDING = "限额待证"
_BREACH_TEMPLATE = (
    "Bad：预算超限。路径 {path} 已用尽 {unit}（已用 {spent} / 限额 {limit}，"
    f"{_LIMITS_PENDING}）。已停止互辩、检索与多模型调用，不会继续计费。"
)
_STOPPED_TEMPLATE = "路径 {path} 已停止，不能再启动该耗费用路径。"
_UNKNOWN_PATH = "未知耗费用路径: {path}。合法路径为 debate / rag / model。"
_NO_EVIDENCE = "路径 {path} 无新证据，已停止该路径，不再继续消耗预算。"


@dataclass
class BudgetAmounts:
    """Tokens, call counts, and integer cost units consumed or configured."""

    tokens: int = 0
    calls: int = 0
    cost: int = 0


@dataclass
class BudgetConfig:
    """Enabled metering units and their configurable limits."""

    units: list[str]
    limits: BudgetAmounts


@dataclass
class BudgetBreach:
    """Queryable record of a budget breach. ``bad`` is the literal label Bad."""

    bad: str = "Bad"
    path: str = ""
    reason: str = ""
    unit: str | None = None


@dataclass
class BudgetSnapshot:
    """Spent, limits, remaining, and stop records on the parent snapshot."""

    units: list[str]
    limits: BudgetAmounts
    spent: BudgetAmounts
    remaining: BudgetAmounts
    last_breach: BudgetBreach | None = None
    stopped_paths: list[str] = field(default_factory=list)
    no_new_evidence_paths: list[str] = field(default_factory=list)
    last_stop_reason: str | None = None


@dataclass
class StartDecision:
    """Whether the parent may start one costly-path Activity."""

    allowed: bool
    reason: str = ""
    unit: str | None = None


@dataclass
class CostlyPathRequest:
    """Update payload for starting one budget-gated costly path. No prompts."""

    path: str


@dataclass
class CostlyPathInput:
    """Activity input for a debate / rag / model stub. Contains no secrets."""

    story_id: str
    execution_id: str
    path: str


@dataclass
class CostlyPathResult:
    """Activity result: usage and whether new evidence was produced."""

    path: str
    has_new_evidence: bool = True
    usage: BudgetAmounts = field(default_factory=BudgetAmounts)
    detail: str = ""


@dataclass
class CostlyPathOutcome:
    """Return value of ``start_costly_path``. Safe to persist in History."""

    started: bool
    path: str
    status: str
    reason: str = ""
    usage: BudgetAmounts | None = None
    has_new_evidence: bool | None = None


def amount_of(amounts: BudgetAmounts, unit: str) -> int:
    """Return the integer amount for one metering unit."""

    if unit == "tokens":
        return amounts.tokens
    if unit == "calls":
        return amounts.calls
    if unit == "cost":
        return amounts.cost
    raise ValueError(f"未知计量单位: {unit}")


def remaining_of(limits: BudgetAmounts, spent: BudgetAmounts, unit: str) -> int:
    """Return non-negative remaining quota for one unit."""

    return max(0, amount_of(limits, unit) - amount_of(spent, unit))


def remaining_amounts(config: BudgetConfig, spent: BudgetAmounts) -> BudgetAmounts:
    """Compute remaining amounts. Disabled units report 0."""

    enabled = set(config.units)
    return BudgetAmounts(
        tokens=remaining_of(config.limits, spent, "tokens") if "tokens" in enabled else 0,
        calls=remaining_of(config.limits, spent, "calls") if "calls" in enabled else 0,
        cost=remaining_of(config.limits, spent, "cost") if "cost" in enabled else 0,
    )


def exhausted_unit(config: BudgetConfig, spent: BudgetAmounts) -> str | None:
    """Return the first enabled unit whose remaining quota is 0, if any."""

    for unit in config.units:
        if remaining_of(config.limits, spent, unit) <= 0:
            return unit
    return None


def add_usage(spent: BudgetAmounts, usage: BudgetAmounts) -> BudgetAmounts:
    """Return accumulated usage. Negative incoming amounts are treated as 0."""

    return BudgetAmounts(
        tokens=spent.tokens + max(0, usage.tokens),
        calls=spent.calls + max(0, usage.calls),
        cost=spent.cost + max(0, usage.cost),
    )


def is_costly_path(path: str) -> bool:
    """Return True when ``path`` is debate, rag, or model."""

    return path in COSTLY_PATHS


def breach_reason(path: str, unit: str, spent: BudgetAmounts, limits: BudgetAmounts) -> str:
    """Build the operator-visible Bad reason for an exhausted unit."""

    return _BREACH_TEMPLATE.format(
        path=path,
        unit=unit,
        spent=amount_of(spent, unit),
        limit=amount_of(limits, unit),
    )


def no_new_evidence_reason(path: str) -> str:
    """Build the operator-visible reason for stopping a path with no new evidence."""

    return _NO_EVIDENCE.format(path=path)


def decide_start(
    config: BudgetConfig,
    spent: BudgetAmounts,
    stopped_paths: tuple[str, ...] | list[str],
    path: str,
) -> StartDecision:
    """Decide whether the next costly-path Activity may start."""

    if not is_costly_path(path):
        return StartDecision(allowed=False, reason=_UNKNOWN_PATH.format(path=path))
    unit = exhausted_unit(config, spent)
    if unit is not None:
        return StartDecision(
            allowed=False,
            reason=breach_reason(path, unit, spent, config.limits),
            unit=unit,
        )
    if path in stopped_paths:
        return StartDecision(allowed=False, reason=_STOPPED_TEMPLATE.format(path=path))
    return StartDecision(allowed=True)


def build_budget_snapshot(
    config: BudgetConfig,
    spent: BudgetAmounts,
    *,
    stopped_paths: tuple[str, ...] | list[str],
    no_new_evidence_paths: tuple[str, ...] | list[str],
    last_breach_path: str | None = None,
    last_breach_unit: str | None = None,
    last_breach: BudgetBreach | None = None,
    last_stop_reason: str | None = None,
) -> BudgetSnapshot:
    """Assemble the queryable budget view from ledger state."""

    breach = last_breach
    if breach is None and last_breach_path and last_breach_unit:
        breach = BudgetBreach(
            bad="Bad",
            path=last_breach_path,
            unit=last_breach_unit,
            reason=breach_reason(last_breach_path, last_breach_unit, spent, config.limits),
        )
    return BudgetSnapshot(
        units=list(config.units),
        limits=config.limits,
        spent=spent,
        remaining=remaining_amounts(config, spent),
        last_breach=breach,
        stopped_paths=list(stopped_paths),
        no_new_evidence_paths=list(no_new_evidence_paths),
        last_stop_reason=last_stop_reason,
    )
