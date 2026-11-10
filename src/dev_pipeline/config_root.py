"""Validate the required config-root path. This module performs filesystem I/O.

Workflow modules must not import this file. Call it from the worker entrypoint
and from the validate_config_root activity only.
"""

from __future__ import annotations

from pathlib import Path

from dev_pipeline.errors import ConfigRootError

_MISSING = "配置根目录是必填参数，不能省略，也不会回退到当前目录或默认路径。"
_NOT_ABSOLUTE = "配置根目录必须是绝对路径，已拒绝相对路径: {path}"
_MISSING_PATH = "配置根目录不存在: {path}。不会当作“可能已存在”继续执行。"
_NOT_DIR = "配置根目录不是目录: {path}"


def validate_config_root_path(config_root: str | None) -> str:
    """Return the resolved absolute config-root, or fail visibly.

    A missing, relative, or non-directory path is never treated as optional.
    The directory must already exist; this function does not create it.
    """

    if config_root is None or not str(config_root).strip():
        raise ConfigRootError(_MISSING)
    raw = str(config_root).strip()
    path = Path(raw)
    if not path.is_absolute():
        raise ConfigRootError(_NOT_ABSOLUTE.format(path=raw))
    try:
        exists = path.exists()
        is_dir = path.is_dir()
    except OSError as exc:
        raise ConfigRootError(f"配置根目录无法访问: {raw}（{exc}）") from exc
    if not exists:
        raise ConfigRootError(_MISSING_PATH.format(path=raw))
    if not is_dir:
        raise ConfigRootError(_NOT_DIR.format(path=raw))
    return str(path)
