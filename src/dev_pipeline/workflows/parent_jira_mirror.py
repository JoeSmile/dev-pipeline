"""Mirror parent stage changes onto the corresponding Jira issue."""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

from dev_pipeline.jira_mirror import JiraLagMarkerInput, JiraStageMirrorInput, JiraStageMirrorResult
from dev_pipeline.workflows.parent_state import ParentState

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.jira_mirror import mark_jira_mirror_lag, mirror_parent_stage

MIRROR_TIMEOUT = timedelta(seconds=30)
MIRROR_RETRY = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=10),
)
LAG_MARKER_TIMEOUT = timedelta(seconds=30)
LAG_MARKER_RETRY = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=5),
)
_LAG_FALLBACK = "Jira 阶段镜像失败"


def lag_reason_from_error(exc: BaseException) -> str:
    """Extract a Chinese, secret-free reason from a failed mirror Activity."""

    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = getattr(current, "message", None)
        text = str(message or current)
        if any(token in text for token in ("Jira", "jira.toml", "令牌", "无法连接", "镜像")):
            return text
        current = getattr(current, "cause", None) or current.__cause__
    return _LAG_FALLBACK


class ParentJiraMirrorMixin:
    """Schedule Jira mirror Activities after parent stage changes."""

    _state: ParentState

    async def _after_stage_change(self) -> None:
        """Mirror the current parent stage, or record 镜像滞后 after retries."""
        await self._mirror_current_stage()

    async def _mirror_current_stage(self) -> None:
        """Call the mirror Activity when an Intake Jira key is present."""
        state = self._state
        issue_key = state.jira_issue_key()
        if issue_key is None or state.input is None:
            return
        payload = JiraStageMirrorInput(
            issue_key=issue_key,
            stage=state.stage.value,
            execution_id=state.input.execution_id,
            config_root=state.input.config_root,
        )
        try:
            result: JiraStageMirrorResult = await workflow.execute_activity(
                mirror_parent_stage,
                payload,
                start_to_close_timeout=MIRROR_TIMEOUT,
                retry_policy=MIRROR_RETRY,
            )
        except ActivityError as exc:
            reason = lag_reason_from_error(exc)
            state.record_jira_mirror_lag(reason)
            await self._try_mark_lag(issue_key, reason)
            return
        state.record_jira_mirror_success(result.stage)

    async def _try_mark_lag(self, issue_key: str, reason: str) -> None:
        """Best-effort lag comment; failures must not fail the Workflow."""
        state = self._state
        assert state.input is not None
        try:
            await workflow.execute_activity(
                mark_jira_mirror_lag,
                JiraLagMarkerInput(
                    issue_key=issue_key,
                    stage=state.stage.value,
                    execution_id=state.input.execution_id,
                    config_root=state.input.config_root,
                    reason=reason,
                ),
                start_to_close_timeout=LAG_MARKER_TIMEOUT,
                retry_policy=LAG_MARKER_RETRY,
            )
        except (ActivityError, ApplicationError):
            return
