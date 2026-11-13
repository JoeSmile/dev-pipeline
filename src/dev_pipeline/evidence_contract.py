"""DP-32 evidence-on-disk contract: path rules, UTF-8 encoding, and minimum fields.

This module is the single source for the on-disk evidence contract.
Verification evidence (DP-24) uses the minimum fields defined here and
must not invent a second format. Other already-landed evidence writers
(Story Retro daily notes and candidates, remote-write-rejection.md)
share the encoding and write/read helpers, but keep their own document
schemas.

progress.log and heartbeat.json are different artifacts: they may reuse
the UTF-8 write helpers, but must not be forced into the verify-evidence
field set.

The module is safe for workflow imports. It does no I/O at import time.
write_text_utf8 / read_text_utf8 / append_text_utf8 are for Activity and
backend code only; workflow modules must not call them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

TEXT_ENCODING = "utf-8"
# Evidence files are BOM-less UTF-8. A BOM would make POSIX readers see
# a leading U+FEFF, and would not protect against a GBK/cp936 rewrite.
UTF8_BOM = b"\xef\xbb\xbf"

DEFAULT_EVIDENCE_RELPATH = "evidence/verify.md"

FIELD_RED_LINE = "对应红线"
FIELD_COMMAND = "命令或现象"
FIELD_RESULT = "结果"
FIELD_EVIDENCE_PATH = "证据路径"
FIELD_TICKET_ID = "ticket_id"
FIELD_EXECUTION_ID = "execution_id"

VERIFY_MIN_FIELDS = (
    FIELD_RED_LINE,
    FIELD_COMMAND,
    FIELD_RESULT,
    FIELD_EVIDENCE_PATH,
    FIELD_TICKET_ID,
    FIELD_EXECUTION_ID,
)

# Chinese plus symbols that a Windows console code page must not mojibake.
CONTRACT_SAMPLE_TEXT = "红线 ↔ 证据「验收」— 通过"


def evidence_relpath(requested: str = "", *, default: str = DEFAULT_EVIDENCE_RELPATH) -> str:
    """Return the in-tree evidence path, defaulting to the DP-32 contract path."""

    text = requested.strip()
    return text or default


def encode_utf8(text: str) -> bytes:
    """Encode *text* as BOM-less UTF-8 bytes."""

    return text.encode(TEXT_ENCODING)


def decode_utf8(data: bytes) -> str:
    """Decode UTF-8 bytes. A leading BOM, if present, is stripped."""

    if data.startswith(UTF8_BOM):
        data = data[len(UTF8_BOM) :]
    return data.decode(TEXT_ENCODING)


def missing_verify_min_fields(text: str) -> tuple[str, ...]:
    """Return any required verify-evidence fields that are absent from *text*."""

    return tuple(field for field in VERIFY_MIN_FIELDS if field not in text)


def write_text_utf8(path: str | Path, text: str) -> str:
    """Write *text* as BOM-less UTF-8, independent of locale or console encoding.

    The file is written as raw UTF-8 bytes (LF preserved). Callers in
    Activity/backend code should use this instead of a locale-default
    text write, which on Windows may be cp936/GBK.

    Args:
        path: Destination file. Parent directories are created as needed.
        text: Unicode text to store.

    Returns:
        The destination path as a string.
    """

    from pathlib import Path as PathType

    destination = PathType(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(encode_utf8(text))
    return str(destination)


def append_text_utf8(path: str | Path, text: str) -> str:
    """Append *text* as BOM-less UTF-8 bytes. Parents are created as needed."""

    from pathlib import Path as PathType

    destination = PathType(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("ab") as handle:
        handle.write(encode_utf8(text))
    return str(destination)


def read_text_utf8(path: str | Path) -> str:
    """Read a UTF-8 evidence file. A leading BOM, if present, is stripped."""

    from pathlib import Path as PathType

    return decode_utf8(PathType(path).read_bytes())


__all__ = [
    "CONTRACT_SAMPLE_TEXT",
    "DEFAULT_EVIDENCE_RELPATH",
    "FIELD_COMMAND",
    "FIELD_EVIDENCE_PATH",
    "FIELD_EXECUTION_ID",
    "FIELD_RED_LINE",
    "FIELD_RESULT",
    "FIELD_TICKET_ID",
    "TEXT_ENCODING",
    "UTF8_BOM",
    "VERIFY_MIN_FIELDS",
    "append_text_utf8",
    "decode_utf8",
    "encode_utf8",
    "evidence_relpath",
    "missing_verify_min_fields",
    "read_text_utf8",
    "write_text_utf8",
]
