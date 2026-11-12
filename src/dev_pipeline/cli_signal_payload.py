"""Build and validate CLI Signal payloads using existing human-signal schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from dev_pipeline.cli_ids import optional_text
from dev_pipeline.errors import PipelineUsageError
from dev_pipeline.signal_schemas import HUMAN_SIGNAL_NAMES, parse_human_signal

_INVALID_JSON = "{label} 不是合法 JSON 对象或数组: {exc}"
_PAYLOAD_NOT_OBJECT = "--payload / --payload-file 必须是 JSON 对象。"
_CHILD_SIGNALS = ("retry_ticket", "drop_ticket")
CLI_SIGNAL_NAMES = HUMAN_SIGNAL_NAMES + _CHILD_SIGNALS


def build_signal_payload(name: str, args: argparse.Namespace) -> object:
    """Build and validate the payload. Raises PipelineUsageError on bad input."""

    mapping = _load_payload_object(args)
    _apply_field_flags(mapping, args)
    if name in _CHILD_SIGNALS:
        ticket_id = mapping.get("ticket_id")
        if not isinstance(ticket_id, str) or not ticket_id.strip():
            raise PipelineUsageError(f"{name} 缺少必填字段: ticket_id")
        return ticket_id.strip()
    if name == "authorize_test_deploy" and not mapping:
        _parsed, reason = parse_human_signal(name, None)
        if reason is not None:
            raise PipelineUsageError(reason)
        return None
    if name == "reopen" and "execution_id" not in mapping:
        new_id = optional_text(getattr(args, "new_execution_id", None))
        if new_id:
            mapping["execution_id"] = new_id
    _parsed, reason = parse_human_signal(name, mapping if mapping else None)
    if reason is not None:
        raise PipelineUsageError(reason)
    return mapping


def _load_payload_object(args: argparse.Namespace) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    raw = optional_text(args.payload)
    if raw is not None:
        mapping.update(_parse_object(raw, "--payload"))
    path = optional_text(args.payload_file)
    if path is not None:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise PipelineUsageError(f"无法读取 --payload-file {path}: {exc}") from exc
        mapping.update(_parse_object(text, "--payload-file"))
    return mapping


def _parse_object(raw: str, label: str) -> dict[str, Any]:
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineUsageError(_INVALID_JSON.format(label=label, exc=exc)) from exc
    if not isinstance(loaded, dict):
        raise PipelineUsageError(_PAYLOAD_NOT_OBJECT)
    return dict(loaded)


def _apply_field_flags(mapping: dict[str, Any], args: argparse.Namespace) -> None:
    flags = {
        "in_scope": optional_text(args.in_scope),
        "out_of_scope": optional_text(args.out_of_scope),
        "locked_by": optional_text(args.locked_by),
        "tradeoff": optional_text(args.tradeoff),
        "decided_by": optional_text(args.decided_by),
        "notes": optional_text(args.notes),
        "reason": optional_text(args.reason),
        "ticket_id": optional_text(args.ticket_id),
        "decision": optional_text(args.decision),
        "candidate_id": optional_text(args.candidate_id),
        "target": optional_text(args.target),
        "evidence_citation": optional_text(args.evidence_citation),
        "submitted_by": optional_text(args.submitted_by),
        "execution_id": optional_text(args.new_execution_id),
    }
    for key, value in flags.items():
        if value is not None:
            mapping[key] = value
    raw_hypotheses = optional_text(args.hypotheses)
    if raw_hypotheses is not None:
        mapping["hypotheses"] = _parse_json_value(raw_hypotheses, "--hypotheses")
    raw_tickets = optional_text(args.tickets)
    if raw_tickets is not None:
        mapping["tickets"] = _parse_json_value(raw_tickets, "--tickets")
    raw_accepted = optional_text(args.accepted)
    if raw_accepted is not None:
        mapping["accepted"] = _parse_bool(raw_accepted, "--accepted")
    raw_automatic = optional_text(args.automatic)
    if raw_automatic is not None:
        mapping["automatic"] = _parse_bool(raw_automatic, "--automatic")


def _parse_json_value(raw: str, label: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise PipelineUsageError(_INVALID_JSON.format(label=label, exc=exc)) from exc


def _parse_bool(raw: str, label: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"true", "1", "yes", "y", "是"}:
        return True
    if lowered in {"false", "0", "no", "n", "否"}:
        return False
    raise PipelineUsageError(f"{label} 必须是 true/false（或 是/否）")


__all__ = [
    "CLI_SIGNAL_NAMES",
    "build_signal_payload",
]
