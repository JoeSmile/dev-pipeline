"""Live official-Temporal proof for the DP-28 ops CLI: start, signal, status, follow.

Skipped unless TEMPORAL_TARGET is set. Prefer an already-running official
server (for example host.docker.internal:7233) instead of binding another 7233.
Uses a throwaway authorized tree and the in-memory fake Jira. Does not call a
live Cursor CLI or read a real CURSOR_API_KEY. Does not write a real Jira issue
on weiperfect007. Assertions use Query/status; a business-running parent is
aborted afterwards. Does not await handle.result() or assert Temporal COMPLETED.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker
from tests.sandbox_fixtures import write_workspace_toml
from tests.stub_activities import StubController
from tests.workflow_helpers import (
    advance_to_implementing,
    complete_ticket,
    story,
    wait_snapshot,
    wait_terminal,
)

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.cli import parse_cli_args, run_start
from dev_pipeline.cli_signal import run_signal
from dev_pipeline.cli_status import run_status
from dev_pipeline.cli_status_follow import run_status_follow
from dev_pipeline.constants import CLIENT_CONNECT_TIMEOUT, DEFAULT_TASK_QUEUE
from dev_pipeline.errors import PipelineSignalError
from dev_pipeline.models import AbortRequest, story_workflow_id
from dev_pipeline.progress_log import (
    append_progress_step,
    heartbeat_path,
    progress_log_path,
    write_heartbeat_file,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow
from dev_pipeline.workspace import reset_default_claim_store

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.temporal_server,
]

SKIP_REASON = "需要已启动的 Temporal 服务：设置环境变量 TEMPORAL_TARGET，例如 localhost:7233"


@pytest.fixture
async def live_client() -> AsyncIterator[Client]:
    target = os.environ.get("TEMPORAL_TARGET", "").strip()
    if not target:
        pytest.skip(SKIP_REASON)
    client = await Client.connect(target, connect_timeout=CLIENT_CONNECT_TIMEOUT)
    yield client


def _unique_jira_key() -> str:
    # Synthetic DP key that this test never creates on the real Jira site.
    return f"DP-928{uuid.uuid4().int % 100000:05d}"


async def _start_worker(
    client: Client,
    tmp_config_root: Path,
    controller: StubController,
    *,
    task_queue: str,
) -> Worker:
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-{task_queue[-8:]}")
    starter = ReopenStarter(client)
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    return worker


async def _finish(handle: object, worker: Worker) -> None:
    try:
        desc = await handle.describe()  # type: ignore[union-attr]
        if desc.status not in {
            WorkflowExecutionStatus.COMPLETED,
            WorkflowExecutionStatus.TERMINATED,
            WorkflowExecutionStatus.CANCELED,
            WorkflowExecutionStatus.FAILED,
        }:
            await handle.signal(DevPipelineWorkflow.abort, AbortRequest(reason="live-cleanup"))
            try:
                await wait_terminal(handle, timeout=30.0)  # type: ignore[arg-type]
            except Exception:
                pass
    finally:
        await worker.__aexit__(None, None, None)
        reset_default_claim_store()


def _connect(client: Client):
    async def connect(target: str, *, connect_timeout: object) -> object:
        assert target
        assert connect_timeout is not None
        return client

    return connect


async def test_live_cli_start_signal_status_and_rejected_signal(
    live_client: Client, tmp_config_root: Path
) -> None:
    controller = StubController()
    suffix = uuid.uuid4().hex[:8]
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-ops-{suffix}"
    worker = await _start_worker(live_client, tmp_config_root, controller, task_queue=task_queue)
    jira_key = _unique_jira_key()
    execution_id = f"exec-ops-{suffix}"
    handle = None
    try:
        start_args = parse_cli_args(
            [
                "start",
                "--jira-key",
                jira_key,
                "--config-root",
                str(tmp_config_root),
                "--execution-id",
                execution_id,
                "--task-queue",
                task_queue,
                "--temporal-target",
                os.environ["TEMPORAL_TARGET"],
            ]
        )
        result = await run_start(start_args, connect=_connect(live_client))
        assert result.jira_key == jira_key
        assert result.workflow_id == story_workflow_id(jira_key, execution_id)
        handle = live_client.get_workflow_handle(result.workflow_id)
        await wait_snapshot(handle, lambda item: item.stage is ParentStage.INTAKE)
        status = await run_status(
            parse_cli_args(
                [
                    "status",
                    "--config-root",
                    str(tmp_config_root),
                    "--jira-key",
                    jira_key,
                    "--execution-id",
                    execution_id,
                ]
            ),
            connect=_connect(live_client),
        )
        assert "stage=Intake" in status
        assert "waiting_signal=lock_scope" in status
        assert "正在等候你确认锁定范围" in status
        accepted = await run_signal(
            parse_cli_args(
                [
                    "signal",
                    "lock_scope",
                    "--config-root",
                    str(tmp_config_root),
                    "--jira-key",
                    jira_key,
                    "--execution-id",
                    execution_id,
                    "--in-scope",
                    "登录校验",
                    "--out-of-scope",
                    "支付",
                    "--locked-by",
                    "Joe",
                ]
            ),
            connect=_connect(live_client),
        )
        assert "已接受 Signal lock_scope" in accepted
        assert "stage=ScopeLocked" in accepted
        assert "waiting_signal=decide_scheme" in accepted
        after_status = await run_status(
            parse_cli_args(
                [
                    "status",
                    "--config-root",
                    str(tmp_config_root),
                    "--workflow-id",
                    result.workflow_id,
                ]
            ),
            connect=_connect(live_client),
        )
        assert "stage=ScopeLocked" in after_status
        assert "正在等候你确认方案取舍" in after_status
        with pytest.raises(PipelineSignalError) as exc:
            await run_signal(
                parse_cli_args(
                    [
                        "signal",
                        "lock_scope",
                        "--config-root",
                        str(tmp_config_root),
                        "--workflow-id",
                        result.workflow_id,
                        "--in-scope",
                        "再次锁定",
                        "--out-of-scope",
                        "支付",
                        "--locked-by",
                        "Joe",
                    ]
                ),
                connect=_connect(live_client),
            )
        assert "已被拒绝" in str(exc.value)
        assert "阶段未改变" in str(exc.value)
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.stage is ParentStage.SCOPE_LOCKED
        assert snap.waiting_signal == "decide_scheme"
        history = str(await handle.fetch_history())
        assert "CURSOR_API_KEY" not in history
        # Stub Jira only; this test never creates a real weiperfect007 issue.
        for comment in controller.jira.list_comments(jira_key):
            assert "CURSOR_API_KEY" not in comment.body
    finally:
        if handle is not None:
            await _finish(handle, worker)
        else:
            await worker.__aexit__(None, None, None)
            reset_default_claim_store()


async def test_live_cli_follow_sees_updating_heartbeat(
    live_client: Client, tmp_config_root: Path
) -> None:
    controller = StubController()
    suffix = uuid.uuid4().hex[:8]
    write_workspace_toml(tmp_config_root, tmp_config_root.parent / f"authorized-follow-{suffix}")
    payload = story(tmp_config_root, [complete_ticket("T1")], suffix=suffix)
    task_queue = f"{DEFAULT_TASK_QUEUE}-live-ops-follow-{suffix}"
    starter = ReopenStarter(live_client)
    worker = Worker(
        live_client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*controller.activities(), starter.start_reopened_workflow],
    )
    await worker.__aenter__()
    handle = await live_client.start_workflow(
        DevPipelineWorkflow.run,
        payload,
        id=story_workflow_id(payload.story_id, payload.execution_id),
        task_queue=task_queue,
    )
    try:
        await advance_to_implementing(handle, payload)
        await wait_snapshot(
            handle,
            lambda item: (
                item.stage is ParentStage.IMPLEMENTING
                and any(ticket.started for ticket in item.tickets)
            ),
        )
        knowledge = str(tmp_config_root)
        # Prefer authorized_root knowledge dir when workspace.toml is present.
        from dev_pipeline.workspace_config import resolve_knowledge_root

        knowledge = resolve_knowledge_root(str(tmp_config_root))
        log_path = progress_log_path(knowledge, payload.execution_id)
        beat_path = heartbeat_path(knowledge, payload.execution_id)
        assert log_path is not None and beat_path is not None
        first_at = datetime.now(UTC)
        append_progress_step(log_path, "正在读取授权 worktree", now=first_at)
        write_heartbeat_file(beat_path, "正在读取授权 worktree", now=first_at)
        second_at = first_at + timedelta(seconds=2)

        async def _sleep(_seconds: float) -> None:
            append_progress_step(log_path, "正在跑单测", now=second_at)
            write_heartbeat_file(beat_path, "正在跑单测", now=second_at)

        seen: list[str] = []
        await run_status_follow(
            parse_cli_args(
                [
                    "status",
                    "--config-root",
                    str(tmp_config_root),
                    "--workflow-id",
                    handle.id,
                    "--follow",
                    "--follow-max",
                    "2",
                    "--follow-interval",
                    "0.05",
                ]
            ),
            connect=_connect(live_client),
            sleep=_sleep,
            output=seen.append,
        )
        assert len(seen) == 2
        assert "stage=Implementing" in seen[0]
        assert "stage=Implementing" in seen[1]
        assert "正在读取授权 worktree" in seen[0] or "current_step=" in seen[0]
        assert "正在跑单测" in seen[1]
        first_beat = [
            line for line in seen[0].splitlines() if line.startswith("last_heartbeat_at=")
        ][0]
        second_beat = [
            line for line in seen[1].splitlines() if line.startswith("last_heartbeat_at=")
        ][0]
        assert first_beat != second_beat
        snap = await handle.query(DevPipelineWorkflow.snapshot)
        assert snap.stage is ParentStage.IMPLEMENTING
        history = str(await handle.fetch_history())
        assert "CURSOR_API_KEY" not in history
    finally:
        await _finish(handle, worker)
