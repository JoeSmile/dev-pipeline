"""DP-29: official Temporal target, required config-root, Windows-first docs/scripts."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from dev_pipeline.constants import DEFAULT_TEMPORAL_TARGET, OFFICIAL_TEMPORAL_CONTAINER
from dev_pipeline.worker import main as worker_main

REPO_ROOT = Path(__file__).resolve().parents[1]
DEV_SETUP = REPO_ROOT / "docs" / "dev-setup.md"
README = REPO_ROOT / "README.md"
START_WORKER_PS1 = REPO_ROOT / "scripts" / "start-worker.ps1"
START_WORKER_SH = REPO_ROOT / "scripts" / "start-worker.sh"
TEST_DOCKER_PS1 = REPO_ROOT / "scripts" / "test-docker.ps1"
TEST_DOCKER_SH = REPO_ROOT / "scripts" / "test-docker.sh"

_FORBIDDEN_IN_COPY_PASTE = (
    "-WithTemporal",
    "--with-temporal",
    "--profile temporal",
    "start-dev",
)
_FENCE = re.compile(r"```[a-zA-Z0-9+-]*\n(.*?)```", re.DOTALL)


def test_default_temporal_target_is_official_loopback() -> None:
    assert DEFAULT_TEMPORAL_TARGET == "127.0.0.1:7233"
    assert OFFICIAL_TEMPORAL_CONTAINER == "agent-platform-temporal-1"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_docs_are_windows_first_and_require_config_root() -> None:
    setup = _read(DEV_SETUP)
    readme = _read(README)
    for text in (setup, readme):
        assert "127.0.0.1:7233" in text
        assert "agent-platform-temporal-1" in text
        assert "docker compose run --build --rm test" in text
        assert "--config-root" in text or "-ConfigRoot" in text
        assert "Windows 商店" in text
    assert "agent_platform_start.ps1" in setup
    assert r"D:\projects\kpl agents\agent-platform" in setup
    assert "git@github.com:JoeSmile/dev-pipeline.git" in setup
    assert "-ConfigRoot D:\\pipeline-config" in setup


def test_docs_copy_paste_blocks_do_not_start_ephemeral_temporal() -> None:
    for path in (DEV_SETUP, README):
        text = _read(path)
        for block in _FENCE.findall(text):
            for needle in _FORBIDDEN_IN_COPY_PASTE:
                assert needle not in block, f"{path.name} copy-paste block contains {needle!r}"


def test_start_worker_scripts_point_at_official_temporal_and_require_config_root() -> None:
    for path in (START_WORKER_PS1, START_WORKER_SH):
        text = _read(path)
        assert "127.0.0.1:7233" in text
        assert "agent-platform-temporal-1" in text
        assert "不会启动 Temporal" in text or "never starts a Temporal" in text.lower()
        # Scripts may mention the forbidden commands only to refuse them.
        if "--profile temporal" in text or "start-dev" in text:
            assert "不要" in text


def test_test_docker_scripts_use_build_and_refuse_second_temporal() -> None:
    for path in (TEST_DOCKER_PS1, TEST_DOCKER_SH):
        text = _read(path)
        assert "docker compose run --build --rm test" in text
        assert "host.docker.internal:7233" in text
        assert "不要" in text
        assert "compose --profile temporal run" not in text


def test_start_worker_sh_missing_config_root_exits_2() -> None:
    result = subprocess.run(
        ["bash", str(START_WORKER_SH)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "必填" in combined
    assert "127.0.0.1:7233" in combined
    assert "不要" in combined


def test_start_worker_sh_help_exits_0() -> None:
    result = subprocess.run(
        ["bash", str(START_WORKER_SH), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "config-root" in result.stdout + result.stderr


def test_start_worker_sh_invalid_config_root_exits_2_without_starting_temporal(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "does-not-exist"
    result = subprocess.run(
        ["bash", str(START_WORKER_SH), "--config-root", str(missing)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "不存在" in combined
    assert "不会当作" in combined

    relative = subprocess.run(
        ["bash", str(START_WORKER_SH), "--config-root", "relative-config"],
        check=False,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    assert relative.returncode == 2
    assert "必须是绝对路径" in relative.stdout + relative.stderr

    as_file = tmp_path / "not-a-dir"
    as_file.write_text("x", encoding="utf-8")
    file_result = subprocess.run(
        ["bash", str(START_WORKER_SH), "--config-root", str(as_file)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert file_result.returncode == 2
    assert "不是目录" in file_result.stdout + file_result.stderr


def test_test_docker_sh_refuses_with_temporal() -> None:
    result = subprocess.run(
        ["bash", str(TEST_DOCKER_SH), "--with-temporal"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert "不要" in combined
    assert "127.0.0.1:7233" in combined
    assert "live-history" in combined


def test_worker_main_missing_config_root_exits_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc:
        worker_main([])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err
    assert "必填" in text
    assert "不会回退" in text


def test_worker_main_invalid_config_root_exits_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    missing = tmp_path / "does-not-exist"
    with pytest.raises(SystemExit) as exc:
        worker_main(["--config-root", str(missing)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    text = captured.out + captured.err + caplog.text
    assert "不存在" in text
    assert "不会当作" in text


def test_cli_start_still_defaults_to_official_temporal_and_exit_2_for_config() -> None:
    from dev_pipeline.cli import main, parse_cli_args

    args = parse_cli_args(
        ["start", "--jira-key", "DP-13", "--config-root", r"D:\pipeline-config"]
    )
    assert args.temporal_target == "127.0.0.1:7233"
    with pytest.raises(SystemExit) as exc:
        main(["start", "--jira-key", "DP-13"])
    assert exc.value.code == 2


def test_documented_stub_tests_use_docker_build_without_a_new_temporal() -> None:
    setup = _read(DEV_SETUP)
    assert "docker compose run --build --rm test" in setup
    assert "Windows 商店" in setup
    assert "不必先启动 Temporal" in setup

def test_powershell_scripts_avoid_here_strings_because_repo_is_lf() -> None:
    """Windows PowerShell 5.1 cannot parse here-strings in LF-only files."""
    for path in (START_WORKER_PS1, TEST_DOCKER_PS1):
        content = _read(path)
        assert '@"' not in content, (
            f"{path.name} uses a here-string; keep LF-safe quoted strings"
        )

def test_powershell_scripts_have_utf8_bom_for_windows() -> None:
    """Windows PowerShell 5.1 needs a UTF-8 BOM to parse Chinese in .ps1 files."""
    for path in (START_WORKER_PS1, TEST_DOCKER_PS1):
        raw = path.read_bytes()
        assert raw.startswith(b"\xef\xbb\xbf"), f"{path.name} is missing a UTF-8 BOM"

def test_powershell_scripts_avoid_smart_quotes() -> None:
    """Windows PowerShell 5.1 treats typographic quotes as string delimiters."""
    for path in (START_WORKER_PS1, TEST_DOCKER_PS1):
        content = _read(path)
        assert "“" not in content and "”" not in content, (
            f"{path.name} contains typographic quotes"
        )

