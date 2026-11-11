"""Shared Intake contract used by CLI start and a later listener path."""

from __future__ import annotations

import pytest

from dev_pipeline.errors import PipelineStartError
from dev_pipeline.intake import (
    INTAKE_SOURCE_CLI,
    PENDING_FIELD,
    intake_from_cli,
    story_input_from_intake,
    validate_jira_key,
)


def test_cli_intake_fills_source_and_marks_unused_fields_pending() -> None:
    record = intake_from_cli("DP-13")
    assert record.jira_key == "DP-13"
    assert record.source == INTAKE_SOURCE_CLI
    assert record.source == "cli"
    assert record.reporter == PENDING_FIELD
    assert record.request == PENDING_FIELD
    assert record.has_auto_run_label == PENDING_FIELD
    assert PENDING_FIELD == "待证"


def test_cli_intake_strips_jira_key() -> None:
    assert intake_from_cli("  DP-2  ").jira_key == "DP-2"


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "dp-13", "DP-", "DP", "PROJ-1", "DP-13a", "DP-13-1", "DP-13.1", "XD-13"],
)
def test_invalid_jira_key_fails_in_chinese(raw: str) -> None:
    with pytest.raises(PipelineStartError, match="Jira key") as exc:
        validate_jira_key(raw)
    assert "DP-" in str(exc.value)


def test_story_input_from_cli_intake_uses_jira_key_and_pending_summary() -> None:
    intake = intake_from_cli("DP-13")
    payload = story_input_from_intake(
        intake,
        config_root="/abs/config",
        execution_id="exec-1",
    )
    assert payload.story_id == "DP-13"
    assert payload.execution_id == "exec-1"
    assert payload.config_root == "/abs/config"
    assert payload.tickets == []
    assert payload.summary == "待证"
    assert payload.intake == intake
    assert payload.intake is not None
    assert payload.intake.source == "cli"
