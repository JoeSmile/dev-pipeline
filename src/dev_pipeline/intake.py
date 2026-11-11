"""Pure Intake helpers shared by CLI start and a later listener path.

This module must stay free of filesystem, network, and Temporal I/O so a
future listener can reuse the same contract without pulling CLI behavior
into the Workflow.
"""

from __future__ import annotations

import re

from dev_pipeline.errors import PipelineStartError
from dev_pipeline.models import (
    INTAKE_SOURCE_CLI,
    PENDING_FIELD,
    IntakeRecord,
    StoryInput,
)

_JIRA_KEY = re.compile(r"^DP-\d+$")
_MISSING_KEY = "Jira key 是必填参数，必须是 DP 项目的 issue key，例如 DP-13。"
_INVALID_KEY = "Jira key 非法: {key}。必须是 DP 项目的 issue key，例如 DP-13。"

# Re-export so callers can import the contract from one module.
__all__ = [
    "INTAKE_SOURCE_CLI",
    "PENDING_FIELD",
    "intake_from_cli",
    "story_input_from_intake",
    "validate_jira_key",
]


def validate_jira_key(raw: str | None) -> str:
    """Return a DP issue key, or fail visibly in Chinese.

    Args:
        raw: Candidate Jira key, such as ``DP-13``.

    Returns:
        The stripped Jira key.

    Raises:
        PipelineStartError: The value is missing or is not a DP issue key.
    """

    if raw is None or not str(raw).strip():
        raise PipelineStartError(_MISSING_KEY)
    key = str(raw).strip()
    if not _JIRA_KEY.fullmatch(key):
        raise PipelineStartError(_INVALID_KEY.format(key=key))
    return key


def intake_from_cli(jira_key: str) -> IntakeRecord:
    """Build the shared Intake record for a CLI start.

    Args:
        jira_key: DP issue key supplied on the command line.

    Returns:
        Intake record with ``source=cli`` and unused fields set to ``待证``.
    """

    return IntakeRecord(
        jira_key=validate_jira_key(jira_key),
        reporter=PENDING_FIELD,
        request=PENDING_FIELD,
        source=INTAKE_SOURCE_CLI,
        has_auto_run_label=PENDING_FIELD,
    )


def story_input_from_intake(
    intake: IntakeRecord,
    *,
    config_root: str,
    execution_id: str,
) -> StoryInput:
    """Build a parent start payload from a shared Intake record.

    Args:
        intake: Shared Intake shape filled by CLI or a later listener.
        config_root: Already-validated absolute config-root path.
        execution_id: Parent execution id used in ``story_workflow_id``.

    Returns:
        Story input whose ``story_id`` is the Jira key.
    """

    return StoryInput(
        story_id=intake.jira_key,
        execution_id=execution_id,
        config_root=config_root,
        tickets=[],
        summary=intake.request,
        intake=intake,
    )
