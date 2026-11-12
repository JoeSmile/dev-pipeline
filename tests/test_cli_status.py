"""CLI ``status`` is a thin read-only Query printer, including follow refresh."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.stub_activities import StubController
from tests.workflow_helpers import complete_ticket, start_parent, story

from dev_pipeline.cli import main, parse_cli_args
from dev_pipeline.cli_status import StatusArgs, format_status, run_status
from dev_pipeline.models import story_workflow_id
from dev_pipeline.workflows import DevPipelineWorkflow


def test_parse_status_requires_config_root_and_an_id() -> None:
    with pytest.raises(SystemExit):
        parse_cli_args(["status"])
    with pytest.raises(SystemExit):
        parse_cli_args(["status", "--config-root", "/abs/config"])
    args = parse_cli_args(
        [
            "status",
            "--config-root",
            "/abs/config",
            "--workflow-id",
            "story-DP-13-exec-1",
        ]
    )
    assert isinstance(args, StatusArgs)
    assert args.command == "status"
    assert args.workflow_id == "story-DP-13-exec-1"
    pair = parse_cli_args(
        [
            "status",
            "--config-root",
            "/abs/config",
            "--jira-key",
            "DP-13",
            "--execution-id",
            "exec-1",
        ]
    )
    assert isinstance(pair, StatusArgs)
    assert pair.jira_key == "DP-13"
    assert pair.execution_id == "exec-1"


@pytest.mark.asyncio
async def test_status_cli_prints_query_fields_and_does_not_mutate(
    tmp_config_root: Path,
) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            before = await handle.query(DevPipelineWorkflow.snapshot)
            comments = [item.body for item in controller.jira.list_comments(payload.story_id)]
            files_before = {
                path: path.stat().st_mtime for path in tmp_config_root.rglob("*") if path.is_file()
            }
            workflow_id = story_workflow_id(payload.story_id, payload.execution_id)

            class _Client:
                def get_workflow_handle(self, value: str) -> object:
                    assert value == workflow_id
                    return handle

            async def _connect(target: str, *, connect_timeout: object) -> object:
                assert target
                assert connect_timeout is not None
                return _Client()

            out = await run_status(
                parse_cli_args(
                    [
                        "status",
                        "--config-root",
                        str(tmp_config_root),
                        "--workflow-id",
                        workflow_id,
                    ]
                ),
                connect=_connect,
            )
            assert "stage=Intake" in out
            assert "waiting_signal=lock_scope" in out
            assert "正在等候你确认锁定范围" in out
            assert "last_failure_reason=" in out
            assert "jira_mirror_lagged=" in out
            assert "current_step=" in out
            after = await handle.query(DevPipelineWorkflow.snapshot)
            assert after.stage is before.stage
            assert after.waiting_signal == before.waiting_signal
            bodies = [item.body for item in controller.jira.list_comments(payload.story_id)]
            assert bodies == comments
            files_after = {
                path: path.stat().st_mtime for path in tmp_config_root.rglob("*") if path.is_file()
            }
            assert files_after == files_before
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-cli-status"})
        finally:
            await worker.__aexit__(None, None, None)


def test_format_status_mentions_unresponsive_label() -> None:
    snap = SimpleNamespace(
        stage=SimpleNamespace(value="Implementing"),
        waiting_signal=None,
        tickets=[],
        budget=None,
        last_failure_reason=None,
        last_rejection=None,
        jira_mirror=None,
        progress=SimpleNamespace(
            waiting_confirmation=None,
            current_step="正在写代码：实现登录校验",
            last_heartbeat_at="2026-11-12T12:00:00+00:00",
            heartbeat_expired=True,
            unresponsive_label="可能无响应",
            recent_hints=["正在写代码：实现登录校验"],
        ),
    )
    text = format_status(snap)  # type: ignore[arg-type]
    assert "unresponsive_label=可能无响应" in text
    assert "current_step=正在写代码：实现登录校验" in text


def test_parse_status_follow_flags_and_rejects_bad_interval() -> None:
    args = parse_cli_args(
        [
            "status",
            "--config-root",
            "/abs/config",
            "--workflow-id",
            "story-DP-13-exec-1",
            "--follow",
            "--follow-interval",
            "0.5",
            "--follow-until",
            "waiting",
            "--follow-max",
            "3",
        ]
    )
    assert isinstance(args, StatusArgs)
    assert args.follow is True
    assert args.follow_interval == 0.5
    assert args.follow_until == "waiting"
    assert args.follow_max == 3
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(
            [
                "status",
                "--config-root",
                "/abs/config",
                "--workflow-id",
                "story-DP-13-exec-1",
                "--follow-interval",
                "0",
            ]
        )
    assert exc.value.code == 2


def test_main_status_missing_config_root_exits_nonzero_in_chinese(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["status", "--workflow-id", "story-DP-13-exec-1"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "配置根目录是必填参数" in text


def test_format_status_distinguishes_waiting_from_expired() -> None:
    waiting = SimpleNamespace(
        stage=SimpleNamespace(value="Intake"),
        waiting_signal="lock_scope",
        tickets=[],
        budget=None,
        last_failure_reason=None,
        last_rejection=None,
        jira_mirror=None,
        progress=SimpleNamespace(
            waiting_confirmation="正在等候你确认锁定范围",
            current_step="",
            last_heartbeat_at="",
            heartbeat_expired=False,
            unresponsive_label=None,
            recent_hints=[],
        ),
    )
    waiting_text = format_status(waiting)  # type: ignore[arg-type]
    assert "正在等候你确认锁定范围" in waiting_text
    assert "unresponsive_label=可能无响应" not in waiting_text
    expired = SimpleNamespace(
        stage=SimpleNamespace(value="Implementing"),
        waiting_signal=None,
        tickets=[],
        budget=None,
        last_failure_reason=None,
        last_rejection=None,
        jira_mirror=None,
        progress=SimpleNamespace(
            waiting_confirmation=None,
            current_step="正在写代码：实现登录校验",
            last_heartbeat_at="2026-11-12T12:00:00+00:00",
            heartbeat_expired=True,
            unresponsive_label="可能无响应",
            recent_hints=["正在写代码：实现登录校验"],
        ),
    )
    expired_text = format_status(expired)  # type: ignore[arg-type]
    assert "unresponsive_label=可能无响应" in expired_text
    assert "正在等候你确认" not in expired_text
    assert "current_step=正在写代码：实现登录校验" in expired_text
    assert "last_heartbeat_at=2026-11-12T12:00:00+00:00" in expired_text
    assert "recent_hints=" in expired_text


def test_main_status_unreachable_exits_nonzero(
    tmp_config_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def connect(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection refused")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "status",
                "--config-root",
                str(tmp_config_root),
                "--workflow-id",
                "story-DP-13-missing",
            ],
            connect=connect,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "无法连接 Temporal" in err
    assert "connection refused" in err
