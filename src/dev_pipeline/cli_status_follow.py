"""Continuous read-only refresh for ``dev-pipeline status --follow``.

This loop only queries and prints. It does not change Workflow state, write
Jira, write files, or auto-pass a waiting Signal. The default refresh
interval reuses the 待证 heartbeat placeholder (2 seconds) and is not a
signed SLA.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TextIO

from dev_pipeline.cli_status import StatusArgs, StatusConnectFn, run_status
from dev_pipeline.progress import DEFAULT_HEARTBEAT_INTERVAL

# 待证: follow refresh interval placeholder, aligned with the heartbeat default.
DEFAULT_FOLLOW_INTERVAL_SECONDS = DEFAULT_HEARTBEAT_INTERVAL.total_seconds()
FOLLOW_UNTIL_CHOICES = ("interrupt", "waiting", "stage-change", "expired")


def should_stop_follow(
    *,
    until: str,
    index: int,
    first_stage: str | None,
    stage: str,
    waiting_confirmation: str | None,
    heartbeat_expired: bool,
    follow_max: int | None,
) -> bool:
    """Return True when follow should stop after printing this observation."""

    if follow_max is not None and index >= follow_max:
        return True
    if until == "waiting":
        return bool(waiting_confirmation)
    if until == "expired":
        return bool(heartbeat_expired)
    if until == "stage-change":
        if index <= 1 or first_stage is None:
            return False
        return stage != first_stage
    return False


def format_follow_observation(index: int, body: str, *, observed_at: str = "") -> str:
    """Frame one follow refresh so operators can see updates accumulate."""

    stamp = f" observed_at={observed_at}" if observed_at else ""
    return f"----- 跟随 #{index}{stamp} -----\n{body}"


def observation_hints(body: str) -> tuple[str, str | None, bool]:
    """Extract stage, waiting confirmation, and expiry from a status block."""

    stage = ""
    waiting: str | None = None
    expired = False
    for line in body.splitlines():
        if line.startswith("stage="):
            stage = line.split("=", 1)[1]
        elif line.startswith("waiting_confirmation="):
            value = line.split("=", 1)[1]
            waiting = value or None
        elif line.startswith("heartbeat_expired="):
            expired = line.split("=", 1)[1].strip().lower() == "true"
        elif line.startswith("unresponsive_label="):
            if line.split("=", 1)[1]:
                expired = True
    return stage, waiting, expired


async def run_status_follow(
    args: StatusArgs,
    *,
    connect: StatusConnectFn | None = None,
    sleep: Callable[[float], Awaitable[None]] | None = None,
    output: Callable[[str], None] | None = None,
    stream: TextIO | None = None,
    clock: Callable[[], datetime] | None = None,
) -> str:
    """Refresh status until interrupted or a documented stop condition.

    Each iteration is a read-only Query. No stage, Jira, or file writes.
    """

    sleeper = sleep or asyncio.sleep
    last = ""
    first_stage: str | None = None
    index = 0
    while True:
        body = await run_status(args, connect=connect)
        index += 1
        observed = clock().isoformat() if clock is not None else ""
        last = format_follow_observation(index, body, observed_at=observed)
        if output is not None:
            output(last)
        else:
            dest = stream or sys.stdout
            dest.write(last if last.endswith("\n") else last + "\n")
            dest.flush()
        stage, waiting, expired = observation_hints(body)
        if first_stage is None:
            first_stage = stage or None
        if should_stop_follow(
            until=args.follow_until,
            index=index,
            first_stage=first_stage,
            stage=stage,
            waiting_confirmation=waiting,
            heartbeat_expired=expired,
            follow_max=args.follow_max,
        ):
            return last
        await sleeper(args.follow_interval)


__all__ = [
    "DEFAULT_FOLLOW_INTERVAL_SECONDS",
    "FOLLOW_UNTIL_CHOICES",
    "format_follow_observation",
    "observation_hints",
    "run_status_follow",
    "should_stop_follow",
]
