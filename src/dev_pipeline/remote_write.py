"""Default-off remote writes and production-target rejection records (DP-31).

Opening a remote PR requires a separate explicit authorization. Until those
conditions are decided they stay ``待证`` and are treated as unauthorized.
This module does not productize an authorization flow and does not deploy
to a test environment (see DP-43). Workflow modules may import the pure
helpers here; filesystem journals live in ``remote_write_store``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from dev_pipeline.constants import ALLOWED_PUBLISH_TARGETS, TEST_PUBLISH_TARGET

# Keep this literal aligned with models.PENDING_FIELD; do not import models here.
PENDING_AUTHORIZATION = "待证"

RemoteWriteAction = Literal["push", "merge", "publish", "open_pr"]
RemoteWriteKind = Literal["unauthorized", "production"]

_PRODUCTION_TOKENS = ("production", "prod", "live")
_REASON_UNAUTH = (
    "默认未授权远程写：不自动 push、不自动合并、不开远程 PR。"
    "开远程 PR 的授权条件仍为待证，视为未授权。"
)
_REASON_PRODUCTION = "禁止生产发布（以及对生产远程的 push / 合并）。已拒绝: {target}"


@dataclass
class RemoteWriteRejection:
    """Queryable record of a rejected remote push, merge, PR, or publish."""

    action: RemoteWriteAction
    target: str
    reason: str
    kind: RemoteWriteKind = "unauthorized"
    performed: bool = False
    authorization: str = PENDING_AUTHORIZATION
    evidence_path: str = ""


@dataclass
class RemoteWriteDecision:
    """Whether a remote write may proceed. Default is denied."""

    allowed: bool
    reason: str = ""
    rejection: RemoteWriteRejection | None = None
    performed: bool = False


def is_pr_authorized(authorization: str | None) -> bool:
    """Return whether opening a remote PR is authorized.

    Authorization conditions are not decided yet, so every value — including
    ``待证``, empty, or any other token — is treated as unauthorized. This
    function does not accept a productized "allow" flag.
    """

    del authorization
    return False


def looks_like_production_target(target: str) -> bool:
    """Return True when *target* names production, prod, or live."""

    lowered = target.strip().lower().replace("_", "").replace("-", "")
    if not lowered:
        return False
    return any(part in lowered for part in _PRODUCTION_TOKENS)


def infer_git_target(argv: list[str] | tuple[str, ...]) -> str:
    """Return the non-flag git operands after the subcommand, if any."""

    tokens = [str(item).strip() for item in argv if str(item).strip()]
    lower = [item.casefold() for item in tokens]
    if "git" in lower:
        tokens = tokens[lower.index("git") + 1 :]
    words = [item for item in tokens if not item.startswith("-")]
    return " ".join(words)


def classify_remote_action(argv: list[str] | tuple[str, ...]) -> RemoteWriteAction | None:
    """Return the remote-write action encoded in a git argv, if forbidden."""

    tokens = [str(item).strip() for item in argv if str(item).strip()]
    lower = [item.casefold() for item in tokens]
    if "git" not in lower:
        return None
    rest = lower[lower.index("git") + 1 :]
    words = [item for item in rest if not item.startswith("-")]
    if "push" in rest or (words and words[0] == "push"):
        return "push"
    if "merge" in rest or "rebase" in rest or (words and words[0] in {"merge", "rebase"}):
        return "merge"
    return None


def production_rejection(
    action: RemoteWriteAction,
    target: str,
    *,
    authorization: str = PENDING_AUTHORIZATION,
    reason: str = "",
) -> RemoteWriteRejection:
    """Build a durable production-target rejection record."""

    label = target.strip() or action
    return RemoteWriteRejection(
        action=action,
        target=target,
        reason=reason or _REASON_PRODUCTION.format(target=label),
        kind="production",
        performed=False,
        authorization=authorization.strip() or PENDING_AUTHORIZATION,
    )


def unauthorized_rejection(
    action: RemoteWriteAction,
    target: str = "",
    *,
    authorization: str = PENDING_AUTHORIZATION,
    reason: str = "",
) -> RemoteWriteRejection:
    """Build a durable record for a default-unauthorized remote write."""

    return RemoteWriteRejection(
        action=action,
        target=target,
        reason=reason or _REASON_UNAUTH,
        kind="unauthorized",
        performed=False,
        authorization=authorization.strip() or PENDING_AUTHORIZATION,
    )


def decide_remote_write(
    action: RemoteWriteAction,
    target: str = "",
    *,
    authorization: str | None = None,
) -> RemoteWriteDecision:
    """Decide a remote write without performing it.

    Push, merge, and opening a remote PR are always denied. A publish target
    of ``test`` is not rejected here; test-env deploy itself remains DP-43.
    Any other publish target, or a production-named push / merge remote, is
    rejected as production and must be recorded by the caller.
    """

    auth = (authorization or "").strip() or PENDING_AUTHORIZATION
    if action == "publish":
        normalized = target.strip().lower()
        if looks_like_production_target(target) or (
            normalized and normalized not in ALLOWED_PUBLISH_TARGETS
        ):
            record = production_rejection("publish", target, authorization=auth)
            return RemoteWriteDecision(
                allowed=False, reason=record.reason, rejection=record, performed=False
            )
        if normalized == TEST_PUBLISH_TARGET:
            return RemoteWriteDecision(
                allowed=True,
                reason="测试环境发布目标合法，部署本身由 DP-43 负责，本模块不执行发布。",
                rejection=None,
                performed=False,
            )
        record = production_rejection("publish", target or "(空)", authorization=auth)
        return RemoteWriteDecision(
            allowed=False, reason=record.reason, rejection=record, performed=False
        )

    if looks_like_production_target(target):
        record = production_rejection(action, target, authorization=auth)
        return RemoteWriteDecision(
            allowed=False, reason=record.reason, rejection=record, performed=False
        )
    record = unauthorized_rejection(action, target, authorization=auth)
    return RemoteWriteDecision(
        allowed=False, reason=record.reason, rejection=record, performed=False
    )
