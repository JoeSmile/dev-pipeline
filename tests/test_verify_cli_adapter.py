"""The reserved verifying CLI adapter stays unwired."""

from dev_pipeline.verify_cli_adapter import plan_verify_cli


def test_verify_cli_plan_is_not_wired() -> None:
    plan = plan_verify_cli("/tmp/authorized/T1/exec-1")
    assert plan.wired is False
    assert plan.invoked is False
    assert plan.authorized_worktree.endswith("exec-1")
