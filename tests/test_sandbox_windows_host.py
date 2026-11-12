"""Narrow host test: real Windows filesystem writes in a throwaway worktree.

Skipped on non-Windows hosts (including Linux Docker). Does not write into the
dev-pipeline checkout or any other real project tree.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from dev_pipeline.sandbox import apply_guarded_write


@pytest.mark.skipif(os.name != "nt", reason="requires a real Windows filesystem")
def test_windows_host_write_inside_and_outside_temp_worktree() -> None:
    root = Path(tempfile.mkdtemp(prefix="dp20-sandbox-"))
    try:
        worktree = root / "T1" / "exec-host"
        worktree.mkdir(parents=True)
        allowed = apply_guarded_write(str(worktree), "src\\ok.txt", "inside")
        assert allowed.allowed is True
        assert (worktree / "src" / "ok.txt").read_text(encoding="utf-8") == "inside"
        outside = root / "secret.txt"
        rejected = apply_guarded_write(str(worktree), str(outside), "nope")
        assert rejected.allowed is False
        assert not outside.exists()
        escaped = apply_guarded_write(str(worktree), "..\\secret.txt", "nope")
        assert escaped.allowed is False
        assert not outside.exists()
    finally:
        shutil.rmtree(root, ignore_errors=True)
