"""Explicit payload schemas and pure validation for parent human signals.

Workflows import this module directly: it must stay deterministic and free of I/O.
Signal handlers accept loosely typed payloads so a bad value is rejected in-process
instead of failing Temporal payload conversion and crashing the Workflow.
"""

from __future__ import annotations

from dataclasses import dataclass, fields, is_dataclass
from typing import Any

from dev_pipeline.models import (
    AbortRequest,
    ApproveSpikeRequest,
    AuthorizeTestDeployRequest,
    PromoteRuleRequest,
    ReopenRequest,
    ReviewOverride,
    SchemeDecision,
    ScopeLock,
    TicketSpec,
)

_REVIEW_DECISIONS = frozenset({"approve", "rework", "reject"})


@dataclass(frozen=True)
class SignalSchema:
    """Describe one human signal's payload type and required fields."""

    payload_type: type
    required: tuple[str, ...]
    nonempty: tuple[str, ...] = ()
    bool_fields: tuple[str, ...] = ()


SCHEMAS: dict[str, SignalSchema] = {
    "lock_scope": SignalSchema(
        ScopeLock,
        ("in_scope", "out_of_scope", "locked_by"),
        nonempty=("in_scope", "out_of_scope", "locked_by"),
    ),
    "decide_scheme": SignalSchema(
        SchemeDecision,
        ("has_high_risk_assumption",),
        bool_fields=("has_high_risk_assumption",),
    ),
    "approve_spike": SignalSchema(
        ApproveSpikeRequest,
        ("accepted",),
        bool_fields=("accepted",),
    ),
    "abort": SignalSchema(AbortRequest, ("reason",), nonempty=("reason",)),
    "reopen": SignalSchema(ReopenRequest, ("execution_id",), nonempty=("execution_id",)),
    "review_override": SignalSchema(
        ReviewOverride,
        ("ticket_id", "decision"),
        nonempty=("ticket_id", "decision"),
    ),
    "promote_rule": SignalSchema(
        PromoteRuleRequest,
        ("rule_name",),
        nonempty=("rule_name",),
        bool_fields=("automatic",),
    ),
    "authorize_test_deploy": SignalSchema(AuthorizeTestDeployRequest, ()),
}

HUMAN_SIGNAL_NAMES = tuple(SCHEMAS)


def parse_human_signal(signal: str, payload: object) -> tuple[object | None, str | None]:
    """Parse and validate a human-signal payload.

    Args:
        signal: Signal name, such as ``lock_scope``.
        payload: Dataclass instance, mapping, or raw value from Temporal.

    Returns:
        ``(parsed, None)`` on success, or ``(None, reason)`` when rejected.
        Reasons are Chinese operator-visible messages. Never raises.
    """

    schema = SCHEMAS.get(signal)
    if schema is None:
        return None, f"未知 Signal: {signal}"
    try:
        return _parse_known_signal(signal, schema, payload)
    except (TypeError, ValueError, AttributeError, KeyError):
        return None, f"{signal} 的 payload 无法解析，请按 schema 传入对象"


def _parse_known_signal(
    signal: str, schema: SignalSchema, payload: object
) -> tuple[object | None, str | None]:
    if signal == "authorize_test_deploy" and payload is None:
        return AuthorizeTestDeployRequest(), None
    mapping, error = _as_field_map(signal, schema, payload)
    if error is not None:
        return None, error
    missing = [name for name in schema.required if name not in mapping or mapping[name] is None]
    if missing:
        return None, f"{signal} 缺少必填字段: {', '.join(missing)}"
    type_error = _type_error(signal, schema, mapping)
    if type_error is not None:
        return None, type_error
    built = _build(signal, schema, mapping)
    return built, None


def _as_field_map(
    signal: str, schema: SignalSchema, payload: object
) -> tuple[dict[str, Any], str | None]:
    if is_dataclass(payload) and not isinstance(payload, type):
        if not isinstance(payload, schema.payload_type):
            return {}, (
                f"{signal} 的 payload 类型无效，需要 {schema.payload_type.__name__}"
            )
        return {item.name: getattr(payload, item.name) for item in fields(payload)}, None
    if isinstance(payload, dict):
        return dict(payload), None
    return {}, (
        f"{signal} 的 payload 类型无效，需要 {schema.payload_type.__name__} 或字段映射"
    )


def _type_error(signal: str, schema: SignalSchema, mapping: dict[str, Any]) -> str | None:
    for name in schema.bool_fields:
        if name in mapping and mapping[name] is not None and not isinstance(mapping[name], bool):
            return f"{signal} 的 {name} 必须是 bool"
    for name in schema.nonempty:
        value = mapping.get(name)
        if not isinstance(value, str) or not value.strip():
            return f"{signal} 的 {name} 必须是非空字符串"
    if signal == "review_override":
        decision = mapping.get("decision")
        if decision not in _REVIEW_DECISIONS:
            return "review_override 的 decision 必须是 approve、rework 或 reject"
    if signal == "decide_scheme" and "tickets" in mapping and mapping["tickets"] is not None:
        return _tickets_error(mapping["tickets"])
    if signal == "promote_rule" and "automatic" in mapping and mapping["automatic"] is None:
        return "promote_rule 的 automatic 必须是 bool"
    return None


def _tickets_error(raw: object) -> str | None:
    if not isinstance(raw, list):
        return "decide_scheme 的 tickets 必须是 TicketSpec 列表"
    for index, item in enumerate(raw):
        ticket_id = _ticket_id_of(item)
        if not ticket_id:
            return f"decide_scheme 的 tickets[{index}] 缺少非空 ticket_id"
    return None


def _ticket_id_of(item: object) -> str:
    if isinstance(item, TicketSpec):
        return item.ticket_id.strip()
    if isinstance(item, dict):
        value = item.get("ticket_id", "")
        return value.strip() if isinstance(value, str) else ""
    return ""


def _build(signal: str, schema: SignalSchema, mapping: dict[str, Any]) -> object:
    if signal == "lock_scope":
        return ScopeLock(
            in_scope=str(mapping["in_scope"]).strip(),
            out_of_scope=str(mapping["out_of_scope"]).strip(),
            locked_by=str(mapping["locked_by"]).strip(),
        )
    if signal == "decide_scheme":
        return SchemeDecision(
            has_high_risk_assumption=bool(mapping["has_high_risk_assumption"]),
            tickets=_build_tickets(mapping.get("tickets") or []),
            notes=str(mapping.get("notes") or ""),
        )
    if signal == "approve_spike":
        return ApproveSpikeRequest(accepted=bool(mapping["accepted"]))
    if signal == "abort":
        return AbortRequest(reason=str(mapping["reason"]).strip())
    if signal == "reopen":
        return ReopenRequest(execution_id=str(mapping["execution_id"]).strip())
    if signal == "review_override":
        return ReviewOverride(
            ticket_id=str(mapping["ticket_id"]).strip(),
            decision=mapping["decision"],
        )
    if signal == "promote_rule":
        automatic = mapping.get("automatic", False)
        return PromoteRuleRequest(
            rule_name=str(mapping["rule_name"]).strip(),
            automatic=bool(automatic),
        )
    return AuthorizeTestDeployRequest()


def _build_tickets(raw: object) -> list[TicketSpec]:
    tickets: list[TicketSpec] = []
    if not isinstance(raw, list):
        return tickets
    for item in raw:
        if isinstance(item, TicketSpec):
            tickets.append(item)
            continue
        if isinstance(item, dict):
            depends = item.get("depends_on") or []
            if not isinstance(depends, list):
                depends = []
            tickets.append(
                TicketSpec(
                    ticket_id=str(item.get("ticket_id") or "").strip(),
                    depends_on=[str(dep) for dep in depends],
                )
            )
    return tickets
