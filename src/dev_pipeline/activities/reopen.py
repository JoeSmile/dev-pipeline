"""Start a new independent parent execution after a legal reopen signal."""

from __future__ import annotations

from datetime import timedelta

import structlog
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError

from dev_pipeline.models import ReopenStartInput, ReopenStartResult, StoryInput, story_workflow_id

logger = structlog.get_logger("dev_pipeline.activities.reopen")

_ALREADY_EXISTS = "无法 reopen：目标执行已存在（workflow_id={workflow_id}），不会复用或恢复旧执行。"


class ReopenStarter:
    """Activity bound to an already-connected Temporal client.

    The client is injected by the worker. The start payload contains only
    story identity and a path-valued config_root, never secrets.
    """

    def __init__(self, client: Client) -> None:
        self._client = client

    @activity.defn(name="start_reopened_workflow")
    async def start_reopened_workflow(self, payload: ReopenStartInput) -> ReopenStartResult:
        """Start a new DevPipelineWorkflow with the given execution_id.

        Args:
            payload: Safe start input for the new parent execution.

        Returns:
            Whether the new execution started, plus a Chinese error when it did not.
        """

        workflow_id = story_workflow_id(payload.story_id, payload.execution_id)
        start = StoryInput(
            story_id=payload.story_id,
            execution_id=payload.execution_id,
            config_root=payload.config_root,
            tickets=list(payload.tickets),
            summary=payload.summary,
            intake=payload.intake,
            inherited_scope=payload.inherited_scope,
        )
        try:
            await self._client.start_workflow(
                "DevPipelineWorkflow",
                start,
                id=workflow_id,
                task_queue=payload.task_queue,
                execution_timeout=timedelta(hours=24),
                task_timeout=timedelta(seconds=30),
            )
        except WorkflowAlreadyStartedError:
            logger.warning("reopen 目标 workflow 已存在", workflow_id=workflow_id)
            return ReopenStartResult(
                started=False,
                workflow_id=workflow_id,
                execution_id=payload.execution_id,
                error=_ALREADY_EXISTS.format(workflow_id=workflow_id),
            )
        logger.info(
            "已启动新的父执行",
            workflow_id=workflow_id,
            execution_id=payload.execution_id,
        )
        return ReopenStartResult(
            started=True,
            workflow_id=workflow_id,
            execution_id=payload.execution_id,
        )
