"""Process-local journal for rejected remote writes. Not used by Workflows."""

from __future__ import annotations

from pathlib import Path

from dev_pipeline.evidence_contract import write_text_utf8
from dev_pipeline.remote_write import RemoteWriteRejection

_REJECTIONS: list[RemoteWriteRejection] = []
EVIDENCE_NAME = "remote-write-rejection.md"


def recorded_rejections() -> tuple[RemoteWriteRejection, ...]:
    """Return a copy of rejections recorded in this process."""

    return tuple(_REJECTIONS)


def reset_recorded_rejections() -> None:
    """Clear the process journal. Intended for tests."""

    _REJECTIONS.clear()


def remember_rejection(
    record: RemoteWriteRejection,
    *,
    evidence_dir: str = "",
) -> RemoteWriteRejection:
    """Append *record* to the journal and optionally write a UTF-8 evidence file.

    Args:
        record: The rejection to keep. ``performed`` must stay false.
        evidence_dir: Optional directory for a durable markdown journal. When
            empty, only the process journal is updated.

    Returns:
        The same record, with ``evidence_path`` set when a file is written.
    """

    if evidence_dir.strip():
        record.evidence_path = write_rejection_evidence(record, evidence_dir)
    _REJECTIONS.append(record)
    return record


def write_rejection_evidence(record: RemoteWriteRejection, directory: str) -> str:
    """Write a UTF-8 markdown record under *directory* and return its path."""

    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    path = root / EVIDENCE_NAME
    if path.exists():
        existing = list(root.glob("remote-write-rejection*.md"))
        path = root / f"remote-write-rejection-{len(existing)}.md"
    performed = "是" if record.performed else "否"
    body = (
        "# 远程写拒绝记录\n\n"
        f"- 动作: `{record.action}`\n"
        f"- 目标: `{record.target}`\n"
        f"- 类别: `{record.kind}`\n"
        f"- 授权: `{record.authorization}`\n"
        f"- 已执行: {performed}\n"
        f"- 原因: {record.reason}\n"
    )
    write_text_utf8(path, body)
    return str(path)
