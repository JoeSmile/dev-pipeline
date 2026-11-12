"""Helpers for coding-sandbox tests."""

from __future__ import annotations

from pathlib import Path


def write_workspace_toml(config_root: Path, authorized_root: Path) -> None:
    """Write workspace.toml pointing at an existing authorized root directory."""

    authorized_root.mkdir(parents=True, exist_ok=True)
    escaped = str(authorized_root).replace('\\', '\\\\')
    (config_root / 'workspace.toml').write_text(
        f'authorized_root = "{escaped}"\n',
        encoding='utf-8',
    )
