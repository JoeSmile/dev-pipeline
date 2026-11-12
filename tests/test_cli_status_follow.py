"""Follow mode refreshes status without mutating Workflow, Jira, or files."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.workflow_helpers import complete_ticket, start_parent, story

from dev_pipeline.cli import parse_cli_args
from dev_pipeline.cli_status import StatusArgs
from dev_pipeline.cli_status_follow import run_status_follow, should_stop_follow
from dev_pipeline.models import PipelineSnapshot
from dev_pipeline.progress_log import (
    append_progress_step,
    heartbeat_path,
    progress_log_path,
    write_heartbeat_file,
)
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow


def test_should_stop_follow_conditions() -> None:
    assert should_stop_follow(
        until="interrupt",
        index=2,
        first_stage="Implementing",
        stage="Implementing",
        waiting_confirmation=None,
        heartbeat_expired=False,
        follow_max=2,
    )
    assert not should_stop_follow(
        until="interrupt",
        index=1,
        first_stage="Implementing",
        stage="Implementing",
        waiting_confirmation=None,
        heartbeat_expired=False,
        follow_max=2,
    )
    assert should_stop_follow(
        until="waiting",
        index=1,
        first_stage="Intake",
        stage="Intake",
        waiting_confirmation="正在等候你确认锁定范围",
        heartbeat_expired=False,
        follow_max=None,
    )
    assert should_stop_follow(
        until="expired",
        index=1,
        first_stage="Implementing",
        stage="Implementing",
        waiting_confirmation=None,
        heartbeat_expired=True,
        follow_max=None,
    )
    assert not should_stop_follow(
        until="stage-change",
        index=1,
        first_stage="Intake",
        stage="Intake",
        waiting_confirmation=None,
        heartbeat_expired=False,
        follow_max=None,
    )
    assert should_stop_follow(
        until="stage-change",
        index=2,
        first_stage="Intake",
        stage="ScopeLocked",
        waiting_confirmation=None,
        heartbeat_expired=False,
        follow_max=None,
    )


def _snapshot(
    *, stage: ParentStage, execution_id: str, story_id: str = "DP-28"
) -> PipelineSnapshot:
    return PipelineSnapshot(
        story_id=story_id,
        execution_id=execution_id,
        stage=stage,
        waiting_signal="lock_scope" if stage is ParentStage.INTAKE else None,
    )


@pytest.mark.asyncio
async def test_follow_refreshes_heartbeat_and_hints_without_mutating(
    tmp_config_root: Path,
) -> None:
    execution_id = "exec-follow-1"
    knowledge = str(tmp_config_root)
    log_path = progress_log_path(knowledge, execution_id)
    beat_path = heartbeat_path(knowledge, execution_id)
    assert log_path is not None and beat_path is not None
    first_at = datetime(2026, 11, 12, 12, 0, 0, tzinfo=UTC)
    second_at = first_at + timedelta(seconds=2)
    append_progress_step(log_path, "正在读取授权 worktree", now=first_at)
    write_heartbeat_file(beat_path, "正在读取授权 worktree", now=first_at)
    snap = _snapshot(stage=ParentStage.IMPLEMENTING, execution_id=execution_id)
    queries = {"count": 0}
    signals = {"count": 0}

    class _Handle:
        async def query(self, *_args: object, **_kwargs: object) -> object:
            queries["count"] += 1
            return snap

        async def describe(self) -> object:
            return SimpleNamespace(raw_description=None)

        async def signal(self, *_args: object, **_kwargs: object) -> None:
            signals["count"] += 1

    class _Client:
        def get_workflow_handle(self, value: str) -> object:
            assert value == "story-DP-13-exec-follow-1"
            return _Handle()

    async def _connect(target: str, *, connect_timeout: object) -> object:
        assert target
        assert connect_timeout is not None
        return _Client()

    async def _sleep(_seconds: float) -> None:
        append_progress_step(log_path, "正在跑单测", now=second_at)
        write_heartbeat_file(beat_path, "正在跑单测", now=second_at)

    args = parse_cli_args(
        [
            "status",
            "--config-root",
            str(tmp_config_root),
            "--workflow-id",
            "story-DP-13-exec-follow-1",
            "--follow",
            "--follow-interval",
            "0.01",
            "--follow-max",
            "2",
        ]
    )
    assert isinstance(args, StatusArgs)
    seen: list[str] = []
    text = await run_status_follow(args, connect=_connect, sleep=_sleep, output=seen.append)
    assert len(seen) == 2
    assert seen[0].startswith("----- 跟随 #1")
    assert seen[1].startswith("----- 跟随 #2")
    assert "current_step=正在读取授权 worktree" in seen[0]
    assert "last_heartbeat_at=" in seen[0]
    assert "正在读取授权 worktree" in seen[0]
    assert "current_step=正在跑单测" in seen[1]
    assert "正在跑单测" in seen[1]
    assert "stage=Implementing" in seen[0]
    assert "stage=Implementing" in seen[1]
    first_beat = [line for line in seen[0].splitlines() if line.startswith("last_heartbeat_at=")][0]
    second_beat = [line for line in seen[1].splitlines() if line.startswith("last_heartbeat_at=")][
        0
    ]
    assert first_beat != second_beat
    assert queries["count"] == 2
    assert signals["count"] == 0
    assert "CURSOR_API_KEY" not in text
    # Follow may append progress evidence through the test's own sleep hook;
    # the command itself must not create or rewrite files outside that hook.
    files_after = {
        path: path.stat().st_mtime_ns for path in tmp_config_root.rglob("*") if path.is_file()
    }
    # After the last observation there is no additional write by follow itself.
    # The sleep hook writes between #1 and #2, so files may differ from before.
    assert log_path in files_after and beat_path in files_after
    assert queries["count"] == 2


@pytest.mark.asyncio
async def test_follow_on_running_parent_does_not_mutate_or_pass_gate(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    from tests.stub_activities import StubController

    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, StubController())
        try:
            before = await handle.query(DevPipelineWorkflow.snapshot)
            files_before = {
                path: (path.stat().st_mtime_ns, path.read_bytes())
                for path in tmp_config_root.rglob("*")
                if path.is_file()
            }

            class _Client:
                def get_workflow_handle(self, value: str) -> object:
                    return handle

            async def _connect(target: str, *, connect_timeout: object) -> object:
                return _Client()

            sleeps = {"count": 0}

            async def _sleep(_seconds: float) -> None:
                sleeps["count"] += 1

            args = parse_cli_args(
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
                    "0.01",
                ]
            )
            seen: list[str] = []
            await run_status_follow(args, connect=_connect, sleep=_sleep, output=seen.append)
            assert len(seen) == 2
            assert "stage=Intake" in seen[0]
            assert "正在等候你确认锁定范围" in seen[0]
            assert "unresponsive_label=可能无响应" not in seen[0]
            after = await handle.query(DevPipelineWorkflow.snapshot)
            assert after.stage is before.stage
            assert after.waiting_signal == before.waiting_signal
            files_after = {
                path: (path.stat().st_mtime_ns, path.read_bytes())
                for path in tmp_config_root.rglob("*")
                if path.is_file()
            }
            assert files_after == files_before
            assert sleeps["count"] == 1
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-follow"})
        finally:
            await worker.__aexit__(None, None, None)
