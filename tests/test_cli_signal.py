"""CLI ``signal`` sends existing human Signals and surfaces accept or reject."""

from __future__ import annotations

from pathlib import Path

import pytest
from temporalio.testing import WorkflowEnvironment
from tests.stub_activities import StubController
from tests.workflow_helpers import complete_ticket, start_parent, story, wait_snapshot

from dev_pipeline.cli import main, parse_cli_args
from dev_pipeline.cli_signal import SignalArgs, run_signal
from dev_pipeline.errors import PipelineSignalError
from dev_pipeline.stages import ParentStage
from dev_pipeline.workflows import DevPipelineWorkflow


def test_parse_signal_requires_name_config_root_and_id() -> None:
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["signal"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["signal", "lock_scope", "--workflow-id", "story-DP-13-exec-1"])
    assert exc.value.code == 2
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(["signal", "lock_scope", "--config-root", "/abs/config"])
    assert exc.value.code == 2


def test_parse_signal_rejects_unknown_name_and_missing_payload(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(
            [
                "signal",
                "not_a_signal",
                "--config-root",
                "/abs/config",
                "--workflow-id",
                "story-DP-13-exec-1",
            ]
        )
    assert exc.value.code == 2
    text = capsys.readouterr().err
    assert "未知或不支持的 Signal" in text
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(
            [
                "signal",
                "lock_scope",
                "--config-root",
                "/abs/config",
                "--workflow-id",
                "story-DP-13-exec-1",
                "--in-scope",
                "登录",
            ]
        )
    assert exc.value.code == 2
    text = capsys.readouterr().err
    assert "缺少必填字段" in text
    with pytest.raises(SystemExit) as exc:
        parse_cli_args(
            [
                "signal",
                "retry_ticket",
                "--config-root",
                "/abs/config",
                "--workflow-id",
                "story-DP-13-exec-1",
            ]
        )
    assert exc.value.code == 2
    assert "ticket_id" in capsys.readouterr().err


def test_parse_signal_accepts_lock_scope_flags() -> None:
    args = parse_cli_args(
        [
            "signal",
            "lock_scope",
            "--config-root",
            r"D:\pipeline-config",
            "--workflow-id",
            "story-DP-13-exec-1",
            "--in-scope",
            "登录校验",
            "--out-of-scope",
            "支付",
            "--locked-by",
            "Joe",
        ]
    )
    assert isinstance(args, SignalArgs)
    assert args.name == "lock_scope"
    assert args.payload == {
        "in_scope": "登录校验",
        "out_of_scope": "支付",
        "locked_by": "Joe",
    }
    named = parse_cli_args(
        [
            "signal",
            "--name",
            "lock_scope",
            "--config-root",
            r"D:\pipeline-config",
            "--jira-key",
            "DP-13",
            "--execution-id",
            "exec-1",
            "--in-scope",
            "登录校验",
            "--out-of-scope",
            "支付",
            "--locked-by",
            "Joe",
        ]
    )
    assert isinstance(named, SignalArgs)
    assert named.jira_key == "DP-13"
    assert named.execution_id == "exec-1"


def test_main_invalid_config_root_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "missing-root"
    with pytest.raises(SystemExit) as exc:
        main(
            [
                "signal",
                "lock_scope",
                "--config-root",
                str(missing),
                "--workflow-id",
                "story-DP-13-exec-1",
                "--in-scope",
                "登录校验",
                "--out-of-scope",
                "支付",
                "--locked-by",
                "Joe",
            ]
        )
    assert exc.value.code == 2
    assert "不存在" in capsys.readouterr().err


def test_main_temporal_unreachable_exits_1(
    tmp_config_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    async def connect(*_args: object, **_kwargs: object) -> object:
        raise OSError("connection refused")

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "signal",
                "lock_scope",
                "--config-root",
                str(tmp_config_root),
                "--workflow-id",
                "story-DP-13-exec-1",
                "--in-scope",
                "登录校验",
                "--out-of-scope",
                "支付",
                "--locked-by",
                "Joe",
            ],
            connect=connect,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "无法连接 Temporal" in err
    assert "connection refused" in err
    assert "未被改写" in err


def test_main_workflow_not_found_exits_1(
    tmp_config_root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    class _Handle:
        async def query(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("workflow not found")

        async def signal(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("must not signal when the workflow is missing")

        async def describe(self) -> object:
            raise RuntimeError("workflow not found")

    class _Client:
        def get_workflow_handle(self, value: str) -> object:
            return _Handle()

    async def connect(*_args: object, **_kwargs: object) -> object:
        return _Client()

    with pytest.raises(SystemExit) as exc:
        main(
            [
                "signal",
                "lock_scope",
                "--config-root",
                str(tmp_config_root),
                "--workflow-id",
                "story-DP-13-missing",
                "--in-scope",
                "登录校验",
                "--out-of-scope",
                "支付",
                "--locked-by",
                "Joe",
            ],
            connect=connect,
        )
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "找不到父执行" in err or "查询父执行失败" in err
    assert "story-DP-13-missing" in err


def _connect_for(handle: object):
    class _Client:
        def get_workflow_handle(self, value: str) -> object:
            return handle

    async def connect(*_args: object, **_kwargs: object) -> object:
        return _Client()

    return connect


@pytest.mark.asyncio
async def test_legal_lock_scope_from_cli_advances_stage(tmp_config_root: Path) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            await wait_snapshot(handle, lambda item: item.stage is ParentStage.INTAKE)
            args = parse_cli_args(
                [
                    "signal",
                    "lock_scope",
                    "--config-root",
                    str(tmp_config_root),
                    "--workflow-id",
                    handle.id,
                    "--in-scope",
                    "登录校验",
                    "--out-of-scope",
                    "支付",
                    "--locked-by",
                    "Joe",
                ]
            )
            text = await run_signal(args, connect=_connect_for(handle))
            assert "已接受 Signal lock_scope" in text
            assert "stage=ScopeLocked" in text
            assert "waiting_signal=decide_scheme" in text
            assert "正在等候你确认方案取舍" in text
            snap = await handle.query(DevPipelineWorkflow.snapshot)
            assert snap.stage is ParentStage.SCOPE_LOCKED
            assert snap.waiting_signal == "decide_scheme"
            assert snap.scope is not None
            assert snap.scope.locked_by == "Joe"
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-legal-signal"})
        finally:
            await worker.__aexit__(None, None, None)


@pytest.mark.asyncio
async def test_wrong_stage_signal_is_rejected_and_stage_stays(tmp_config_root: Path) -> None:
    payload = story(tmp_config_root, [complete_ticket("T1")])
    controller = StubController()
    async with await WorkflowEnvironment.start_time_skipping() as env:
        worker, handle = await start_parent(env, payload, controller)
        try:
            await wait_snapshot(handle, lambda item: item.stage is ParentStage.INTAKE)
            first = parse_cli_args(
                [
                    "signal",
                    "lock_scope",
                    "--config-root",
                    str(tmp_config_root),
                    "--workflow-id",
                    handle.id,
                    "--in-scope",
                    "登录校验",
                    "--out-of-scope",
                    "支付",
                    "--locked-by",
                    "Joe",
                ]
            )
            await run_signal(first, connect=_connect_for(handle))
            snap = await wait_snapshot(handle, lambda item: item.stage is ParentStage.SCOPE_LOCKED)
            assert snap.waiting_signal == "decide_scheme"
            with pytest.raises(PipelineSignalError) as exc:
                await run_signal(
                    parse_cli_args(
                        [
                            "signal",
                            "lock_scope",
                            "--config-root",
                            str(tmp_config_root),
                            "--workflow-id",
                            handle.id,
                            "--in-scope",
                            "再次锁定",
                            "--out-of-scope",
                            "支付",
                            "--locked-by",
                            "Joe",
                        ]
                    ),
                    connect=_connect_for(handle),
                )
            err = str(exc.value)
            assert "已被拒绝" in err
            assert "阶段未改变" in err
            assert "lock_scope" in err
            after = await handle.query(DevPipelineWorkflow.snapshot)
            assert after.stage is ParentStage.SCOPE_LOCKED
            assert after.waiting_signal == "decide_scheme"
            assert after.last_rejection is not None
            assert after.last_rejection.signal == "lock_scope"
            # Illegal decide? already locked path is lock_scope again.
            # Also reject decide-scheme missing? covered in parse tests.
            with pytest.raises(PipelineSignalError) as signal_exc:
                await run_signal(
                    parse_cli_args(
                        [
                            "signal",
                            "approve_spike",
                            "--config-root",
                            str(tmp_config_root),
                            "--workflow-id",
                            handle.id,
                            "--accepted",
                            "true",
                        ]
                    ),
                    connect=_connect_for(handle),
                )
            assert "已被拒绝" in str(signal_exc.value)
            still = await handle.query(DevPipelineWorkflow.snapshot)
            assert still.stage is ParentStage.SCOPE_LOCKED
            await handle.signal(DevPipelineWorkflow.abort, {"reason": "stop-reject-signal"})
        finally:
            await worker.__aexit__(None, None, None)
