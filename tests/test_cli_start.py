"""CLI start must enter Intake and fail visibly; never swallow start errors."""

from __future__ import annotations

import uuid
from datetime import timedelta
from pathlib import Path

import pytest
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from tests.history_events import workflow_execution_started_count
from tests.stub_activities import StubController
from tests.workflow_helpers import wait_snapshot

from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.cli import main, parse_cli_args, run_start
from dev_pipeline.constants import (
    CLIENT_CONNECT_TIMEOUT,
    DEFAULT_TASK_QUEUE,
    DEFAULT_TEMPORAL_TARGET,
)
from dev_pipeline.errors import BudgetConfigError, ConfigRootError, PipelineStartError
from dev_pipeline.models import story_workflow_id
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow, TicketWorkflow


def test_parse_start_requires_jira_key_and_config_root() -> None:
    with pytest.raises(SystemExit) as exc:
        parse_cli_args([])
    assert exc.value.code != 0
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["start", "--config-root", "/abs/config"])
    assert exc.value.code != 0
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["start", "--jira-key", "DP-13"])
    assert exc.value.code != 0


def test_parse_start_accepts_required_flags_and_defaults() -> None:
    args = parse_cli_args(["start", "--jira-key", "DP-13", "--config-root", r"D:\pipeline-config"])
    assert args.command == "start"
    assert args.jira_key == "DP-13"
    assert args.config_root == r"D:\pipeline-config"
    assert args.execution_id is None
    assert args.temporal_target == DEFAULT_TEMPORAL_TARGET
    assert args.task_queue == DEFAULT_TASK_QUEUE
    custom = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-2",
            "--config-root",
            "/abs/config",
            "--execution-id",
            "exec-9",
            "--temporal-target",
            "localhost:8233",
            "--task-queue",
            "custom-q",
        ]
    )
    assert custom.execution_id == "exec-9"
    assert custom.temporal_target == "localhost:8233"
    assert custom.task_queue == "custom-q"


def test_main_invalid_jira_key_exits_nonzero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["start", "--jira-key", "PROJ-1", "--config-root", "/abs/config"])
    assert exc.value.code != 0


def test_main_missing_config_root_exits_nonzero_in_chinese(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as exc:
        main(["start", "--jira-key", "DP-13", "--config-root", str(missing)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "不存在" in text
    assert "可能已存在" not in text or "不会当作" in text


async def test_missing_config_root_fails_before_connect(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist"
    args = parse_cli_args(["start", "--jira-key", "DP-13", "--config-root", str(missing)])
    called = False

    async def connect(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("missing config-root must not connect to Temporal")

    with pytest.raises(ConfigRootError, match="不存在") as exc:
        await run_start(args, connect=connect)
    assert "不会当作" in str(exc.value)
    assert called is False


async def test_relative_config_root_fails_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config").mkdir()
    args = parse_cli_args(["start", "--jira-key", "DP-13", "--config-root", "config"])
    with pytest.raises(ConfigRootError, match="必须是绝对路径"):
        await run_start(args)


async def test_missing_budget_config_fails_immediately_in_chinese(tmp_path: Path) -> None:
    root = tmp_path / "empty-config"
    root.mkdir()
    args = parse_cli_args(["start", "--jira-key", "DP-13", "--config-root", str(root)])
    called = False

    async def connect(*_args: object, **_kwargs: object) -> object:
        nonlocal called
        called = True
        raise AssertionError("missing budget.toml must not connect to Temporal")

    with pytest.raises(BudgetConfigError, match="budget.toml") as exc:
        await run_start(args, connect=connect)
    assert "不会使用隐藏默认限额" in str(exc.value) or "不会" in str(exc.value)
    assert called is False


async def test_invalid_budget_config_fails_immediately(tmp_config_root: Path) -> None:
    (tmp_config_root / "budget.toml").write_text("this is not toml {", encoding="utf-8")
    args = parse_cli_args(
        ["start", "--jira-key", "DP-13", "--config-root", str(tmp_config_root)]
    )
    with pytest.raises(BudgetConfigError, match="无法解析"):
        await run_start(args)


async def test_empty_execution_id_is_rejected(tmp_config_root: Path) -> None:
    args = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            "  ",
        ]
    )
    with pytest.raises(PipelineStartError, match="execution_id"):
        await run_start(args)


async def test_duplicate_workflow_id_is_explicit_failure(tmp_config_root: Path) -> None:
    class FakeClient:
        async def start_workflow(self, *_args: object, **kwargs: object) -> object:
            raise WorkflowAlreadyStartedError(
                str(kwargs.get("id", "story-DP-13-exec-1")),
                "DevPipelineWorkflow",
            )

    async def connect(*_args: object, **_kwargs: object) -> object:
        return FakeClient()

    args = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            "exec-1",
        ]
    )
    with pytest.raises(PipelineStartError) as exc:
        await run_start(args, connect=connect)
    text = str(exc.value)
    assert "已存在" in text
    assert story_workflow_id("DP-13", "exec-1") in text
    assert "不会当作" in text
    assert "成功" not in text.replace("不会当作已成功", "")


async def test_start_failure_is_not_swallowed_as_maybe_exists(tmp_config_root: Path) -> None:
    class FakeClient:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("temporal exploded")

    async def connect(*_args: object, **_kwargs: object) -> object:
        return FakeClient()

    args = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            "exec-boom",
        ]
    )
    with pytest.raises(PipelineStartError) as exc:
        await run_start(args, connect=connect)
    text = str(exc.value)
    assert "失败" in text
    assert "temporal exploded" in text
    assert "不会当作" in text


async def test_temporal_unreachable_fails_in_chinese(tmp_config_root: Path) -> None:
    async def connect(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection refused")

    args = parse_cli_args(
        ["start", "--jira-key", "DP-13", "--config-root", str(tmp_config_root)]
    )
    with pytest.raises(PipelineStartError, match="无法连接 Temporal") as exc:
        await run_start(args, connect=connect)
    assert "connection refused" in str(exc.value)
    assert "不会当作" in str(exc.value)


async def test_connect_uses_explicit_timeout(tmp_config_root: Path) -> None:
    seen: dict[str, object] = {}

    async def connect(target: str, *, connect_timeout: timedelta) -> object:
        seen["target"] = target
        seen["connect_timeout"] = connect_timeout

        class FakeClient:
            async def start_workflow(self, *_args: object, **kwargs: object) -> object:
                seen["start"] = kwargs
                return object()

        return FakeClient()

    args = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            "exec-timeout",
            "--temporal-target",
            "temporal.example:7233",
        ]
    )
    result = await run_start(args, connect=connect)
    assert seen["target"] == "temporal.example:7233"
    assert seen["connect_timeout"] == CLIENT_CONNECT_TIMEOUT
    start = seen["start"]
    assert isinstance(start, dict)
    assert start.get("rpc_timeout") is not None
    assert start.get("id_reuse_policy") is WorkflowIDReusePolicy.REJECT_DUPLICATE
    assert start.get("id_conflict_policy") is WorkflowIDConflictPolicy.FAIL
    assert result.workflow_id == story_workflow_id("DP-13", "exec-timeout")
    assert result.execution_id == "exec-timeout"
    assert result.jira_key == "DP-13"


async def _assert_cli_entered_intake(handle: object, result: object, execution_id: str) -> None:
    snap = await wait_snapshot(
        handle,
        lambda item: item.stage is ParentStage.INTAKE and item.story_draft is not None,
    )
    assert result.workflow_id == story_workflow_id("DP-13", execution_id)
    assert snap.story_id == "DP-13"
    assert snap.execution_id == execution_id
    assert snap.stage is ParentStage.INTAKE
    assert snap.waiting_signal == "lock_scope"
    assert snap.intake is not None
    assert snap.intake.jira_key == "DP-13"
    assert snap.intake.source == "cli"
    assert snap.intake.reporter == "待证"
    assert snap.intake.request == "待证"
    assert snap.intake.has_auto_run_label == "待证"
    assert snap.story_draft is not None
    assert snap.story_draft.to_solve == "待证"
    assert snap.story_draft.known_constraints == "待证"
    assert "要解决什么: 待证" in snap.story_draft.missing_items
    assert "已知约束: 待证" in snap.story_draft.missing_items
    assert snap.story_draft.complete is False
    history = await handle.fetch_history()
    assert workflow_execution_started_count(history) >= 1
    assert handle.id == result.workflow_id


async def test_cli_starts_workflow_into_intake_and_history_matches(
    workflow_env: WorkflowEnvironment, tmp_config_root: Path
) -> None:
    task_queue = f"{DEFAULT_TASK_QUEUE}-{uuid.uuid4().hex[:8]}"
    execution_id = f"exec-{uuid.uuid4().hex[:8]}"
    starter = ReopenStarter(workflow_env.client)
    worker = Worker(
        workflow_env.client,
        task_queue=task_queue,
        workflows=[DevPipelineWorkflow, TicketWorkflow],
        activities=[*StubController().activities(), starter.start_reopened_workflow],
    )

    async def connect(*_args: object, **_kwargs: object) -> object:
        return workflow_env.client

    args = parse_cli_args(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            execution_id,
            "--task-queue",
            task_queue,
        ]
    )
    async with worker:
        result = await run_start(args, connect=connect)
        handle = workflow_env.client.get_workflow_handle(result.workflow_id)
        await _assert_cli_entered_intake(handle, result, execution_id)


def test_main_duplicate_id_exits_nonzero(tmp_config_root: Path) -> None:
    class FakeClient:
        async def start_workflow(self, *_args: object, **kwargs: object) -> object:
            raise WorkflowAlreadyStartedError(
                str(kwargs.get("id", "story-DP-13-dup")),
                "DevPipelineWorkflow",
            )

    async def connect(*_args: object, **_kwargs: object) -> object:
        return FakeClient()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "start",
                "--jira-key",
                "DP-13",
                "--config-root",
                str(tmp_config_root),
                "--execution-id",
                "dup",
            ],
            connect=connect,
        )
    assert exc.value.code != 0


def test_main_prints_ids_on_success(
    tmp_config_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class FakeClient:
        async def start_workflow(self, *_args: object, **_kwargs: object) -> object:
            return object()

    async def connect(*_args: object, **_kwargs: object) -> object:
        return FakeClient()

    main(
        [
            "start",
            "--jira-key",
            "DP-13",
            "--config-root",
            str(tmp_config_root),
            "--execution-id",
            "exec-ok",
        ],
        connect=connect,
    )
    out = capsys.readouterr().out
    assert "已进入 Intake" in out
    assert f"workflow_id={story_workflow_id('DP-13', 'exec-ok')}" in out
    assert "execution_id=exec-ok" in out
    assert "jira_key=DP-13" in out
