"""Activity entrypoints. Workflows import these through the Temporal sandbox escape hatch."""

from dev_pipeline.activities.budget import load_budget_config, run_debate, run_model_call, run_rag
from dev_pipeline.activities.config import validate_config_root
from dev_pipeline.activities.jira_mirror import mark_jira_mirror_lag, mirror_parent_stage
from dev_pipeline.activities.reopen import ReopenStarter
from dev_pipeline.activities.story_draft import normalize_story_draft
from dev_pipeline.activities.stubs import (
    implement_ticket,
    publish_to_test_env,
    review_ticket,
    run_spike,
    verify_ticket,
    write_retro_record,
)
from dev_pipeline.activities.workspace import claim_ticket_workspace

STUB_ACTIVITIES = (
    validate_config_root,
    load_budget_config,
    normalize_story_draft,
    mirror_parent_stage,
    mark_jira_mirror_lag,
    run_spike,
    claim_ticket_workspace,
    implement_ticket,
    review_ticket,
    verify_ticket,
    run_debate,
    run_rag,
    run_model_call,
    publish_to_test_env,
    write_retro_record,
)

__all__ = [
    "STUB_ACTIVITIES",
    "ReopenStarter",
    "claim_ticket_workspace",
    "implement_ticket",
    "load_budget_config",
    "mark_jira_mirror_lag",
    "normalize_story_draft",
    "mirror_parent_stage",
    "publish_to_test_env",
    "review_ticket",
    "run_debate",
    "run_model_call",
    "run_rag",
    "run_spike",
    "validate_config_root",
    "verify_ticket",
    "write_retro_record",
]
