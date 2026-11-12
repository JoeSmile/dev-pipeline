"""Pure Intake-to-Story-draft normalization. No filesystem, network, or Temporal I/O."""

from __future__ import annotations

import re
from typing import Protocol

from dev_pipeline.models import PENDING_FIELD, IntakeRecord, StoryDraft

TO_SOLVE_LABEL = "要解决什么"
CONSTRAINTS_LABEL = "已知约束"
OUT_OF_SCOPE_LABEL = "不做什么"
ACCEPTANCE_LABEL = "验收"

CURSOR_CLI_NOT_WIRED = "Cursor CLI Story 草稿路径尚未接入；本票只做确定性规范化，缺项标待证。"

# Longer aliases first so "要解决什么" wins over "要解决", and "验收标准" over "验收".
_FIELD_LABELS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("to_solve", ("要解决什么", "要解决")),
    ("known_constraints", (CONSTRAINTS_LABEL,)),
    ("out_of_scope", (OUT_OF_SCOPE_LABEL,)),
    ("acceptance", ("验收标准", "验收条件", ACCEPTANCE_LABEL)),
)
_ALIAS_TO_FIELD = {alias: field for field, aliases in _FIELD_LABELS for alias in aliases}
_LABEL_PATTERN = "|".join(sorted((alias for alias in _ALIAS_TO_FIELD), key=len, reverse=True))
_LABEL_RE = re.compile(
    rf"(?:^|\n)[ \t]*({_LABEL_PATTERN})[ \t]*[:：][ \t]*",
)

_DRAFT_FIELDS: tuple[tuple[str, str], ...] = (
    ("to_solve", TO_SOLVE_LABEL),
    ("known_constraints", CONSTRAINTS_LABEL),
    ("out_of_scope", OUT_OF_SCOPE_LABEL),
    ("acceptance", ACCEPTANCE_LABEL),
)


def is_missing_value(value: str | None) -> bool:
    """Return True when a field is absent, blank, or already marked 待证."""

    if value is None:
        return True
    text = value.strip()
    return text == "" or text == PENDING_FIELD


def known_text(value: str | None) -> str | None:
    """Return stripped text, or None when the value is a missing marker."""

    if is_missing_value(value):
        return None
    assert value is not None
    return value.strip()


def _extract_labeled(text: str) -> dict[str, str]:
    matches = list(_LABEL_RE.finditer(text))
    if not matches:
        return {}
    found: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        field = _ALIAS_TO_FIELD[match.group(1)]
        if field in found:
            continue
        found[field] = text[match.end() : end].strip()
    preamble = text[: matches[0].start()].strip()
    if "to_solve" not in found and preamble:
        found["to_solve"] = preamble
    elif "to_solve" in found and is_missing_value(found["to_solve"]) and preamble:
        found["to_solve"] = preamble
    return found


def draft_story_from_intake(
    intake: IntakeRecord | None,
    *,
    summary: str = "",
) -> StoryDraft:
    """Turn a raw Intake request into a normalized Story draft.

    Args:
        intake: Shared Intake record, if one was supplied at start.
        summary: Fallback text used only when the raw request is missing.

    Returns:
        A draft covering 要解决什么, 已知约束, and a 缺项列表. Missing
        fields are ``待证``. Acceptance is never invented.
    """

    request = "" if intake is None else intake.request
    labeled = _extract_labeled(request) if known_text(request) is not None else {}
    if labeled:
        to_solve = known_text(labeled.get("to_solve"))
        known_constraints = known_text(labeled.get("known_constraints"))
        out_of_scope = known_text(labeled.get("out_of_scope"))
        acceptance = known_text(labeled.get("acceptance"))
    else:
        to_solve = known_text(request)
        known_constraints = None
        out_of_scope = None
        acceptance = None
    if to_solve is None:
        to_solve = known_text(summary)

    values = {
        "to_solve": to_solve or PENDING_FIELD,
        "known_constraints": known_constraints or PENDING_FIELD,
        "out_of_scope": out_of_scope or PENDING_FIELD,
        "acceptance": acceptance or PENDING_FIELD,
    }
    missing_items = [
        f"{label}: {PENDING_FIELD}"
        for field, label in _DRAFT_FIELDS
        if is_missing_value(values[field])
    ]
    return StoryDraft(
        to_solve=values["to_solve"],
        known_constraints=values["known_constraints"],
        missing_items=missing_items,
        out_of_scope=values["out_of_scope"],
        acceptance=values["acceptance"],
        complete=not missing_items,
    )


def lock_scope_preview(draft: StoryDraft) -> dict[str, str]:
    """Map draft fields to the ``lock_scope`` payload shape. Does not lock scope.

    Args:
        draft: Normalized Story draft recorded during Intake.

    Returns:
        ``in_scope`` / ``out_of_scope`` values, which may still be ``待证``.
    """

    return {"in_scope": draft.to_solve, "out_of_scope": draft.out_of_scope}


class StoryDraftBackend(Protocol):
    """Pluggable draft producer. The Activity uses the deterministic backend."""

    def draft(self, intake: IntakeRecord | None, summary: str) -> StoryDraft:
        """Build a Story draft from Intake fields."""


class DeterministicStoryDraftBackend:
    """Default backend: copy known text and mark gaps ``待证``. No model calls."""

    def draft(self, intake: IntakeRecord | None, summary: str) -> StoryDraft:
        """Return the deterministic normalization of ``intake`` / ``summary``."""

        return draft_story_from_intake(intake, summary=summary)


class CursorCliStoryDraftBackend:
    """Reserved Cursor CLI hook. This ticket does not invoke it or read keys.

    Args:
        config_root: Config-root path a later adapter may use to locate
            ``CURSOR_API_KEY``. This class must not read that file.
        runner: Optional injectable process runner for a later harness.
    """

    def __init__(
        self,
        config_root: str,
        runner: object | None = None,
    ) -> None:
        self._config_root = config_root
        self._runner = runner

    def draft(self, intake: IntakeRecord | None, summary: str) -> StoryDraft:
        """Refuse to pretend a live Cursor CLI call happened.

        Args:
            intake: Unused; accepted so the later hook can share this signature.
            summary: Unused; accepted so the later hook can share this signature.

        Raises:
            RuntimeError: Always, with a Chinese explanation that the path
                is not wired in this ticket.
        """

        raise RuntimeError(CURSOR_CLI_NOT_WIRED)
