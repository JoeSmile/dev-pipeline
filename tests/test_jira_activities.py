"""Activity-level tests: missing config fails visibly and is not a silent success."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.exceptions import ApplicationError

from dev_pipeline.activities.jira_mirror import mark_jira_mirror_lag, mirror_parent_stage
from dev_pipeline.jira_mirror import JiraLagMarkerInput, JiraStageMirrorInput


def _payload(config_root: Path, stage: str = "Intake") -> JiraStageMirrorInput:
    return JiraStageMirrorInput(
        issue_key="DP-9999",
        stage=stage,
        execution_id="exec-1",
        config_root=str(config_root),
    )


@pytest.mark.asyncio
async def test_mirror_activity_missing_config_is_visible_and_non_retryable(
    tmp_path: Path,
) -> None:
    with pytest.raises(ApplicationError) as exc:
        await mirror_parent_stage(_payload(tmp_path))
    assert exc.value.non_retryable is True
    assert "jira.toml" in str(exc.value)
    assert "缺少" in str(exc.value)
    assert "secret" not in str(exc.value).lower()


@pytest.mark.asyncio
async def test_lag_activity_missing_config_is_visible_and_non_retryable(
    tmp_path: Path,
) -> None:
    payload = JiraLagMarkerInput(
        issue_key="DP-9999",
        stage="Intake",
        execution_id="exec-1",
        config_root=str(tmp_path),
        reason="配置根目录缺少 Jira 配置文件 jira.toml",
    )
    with pytest.raises(ApplicationError) as exc:
        await mark_jira_mirror_lag(payload)
    assert exc.value.non_retryable is True
    assert "jira.toml" in str(exc.value)
