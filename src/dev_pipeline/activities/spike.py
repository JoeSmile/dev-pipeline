"""Deterministic Spike activities. I/O stays here; the workflow only orchestrates.

The default ``run_spike`` backend is a stub: it does not read CURSOR_API_KEY,
does not call the Cursor CLI, and does not write target-story code. A future
Cursor CLI / model backend is reserved and left unwired.
"""

from temporalio import activity

from dev_pipeline.models import (
    SPIKE_BACKEND_STUB,
    SPIKE_VERIFIED,
    SpikeConfig,
    SpikeInput,
    SpikeResult,
)
from dev_pipeline.spike_config import load_spike_config_from_path


@activity.defn
async def load_spike_config(config_root: str) -> SpikeConfig:
    """Load optional spike.toml, or the 待证 default timeout."""

    return load_spike_config_from_path(config_root)


@activity.defn
async def run_spike(payload: SpikeInput) -> SpikeResult:
    """Default deterministic spike: record 验证过 without pretending a model ran."""

    activity.logger.info("stub run_spike story=%s backend=%s", payload.story_id, SPIKE_BACKEND_STUB)
    statement = payload.statement.strip() or "待证"
    return SpikeResult(
        conclusion=SPIKE_VERIFIED,
        statement=statement,
        detail="桩 Spike：未调用模型，按可证伪结论记为验证过。不是目标 Story 的正式实现。",
        has_protection=False,
        backend=SPIKE_BACKEND_STUB,
        red_line_failed=False,
    )
