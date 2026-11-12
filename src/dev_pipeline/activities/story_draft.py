"""Intake Story-draft Activity. Deterministic; Cursor CLI is not invoked."""

from temporalio import activity

from dev_pipeline.models import StoryDraft, StoryDraftInput
from dev_pipeline.story_draft import DeterministicStoryDraftBackend


@activity.defn
async def normalize_story_draft(payload: StoryDraftInput) -> StoryDraft:
    """Record a normalized Story draft from Intake, marking gaps as 待证.

    This path is deterministic and does not read secrets from ``config_root``.
    A Cursor CLI adapter exists in ``dev_pipeline.story_draft`` but is not used.

    Args:
        payload: Intake record, optional summary, and config-root path.

    Returns:
        The recorded draft. ``complete`` is false when any required field is 待证.
    """

    activity.logger.info("normalize_story_draft using deterministic backend")
    return DeterministicStoryDraftBackend().draft(payload.intake, payload.summary)
