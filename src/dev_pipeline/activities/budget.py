"""Budget-config load and debate / rag / model stub activities."""

from temporalio import activity

from dev_pipeline.budget import BudgetAmounts, BudgetConfig, CostlyPathInput, CostlyPathResult
from dev_pipeline.budget_config import load_budget_config_from_path


@activity.defn
async def load_budget_config(config_root: str) -> BudgetConfig:
    """Fail visibly when budget.toml is missing or invalid; never hide a default quota."""

    return load_budget_config_from_path(config_root)


@activity.defn
async def run_debate(payload: CostlyPathInput) -> CostlyPathResult:
    """Default debate stub. Real multi-agent debate is a later ticket."""

    activity.logger.info("stub run_debate story=%s", payload.story_id)
    return CostlyPathResult(
        path="debate",
        has_new_evidence=True,
        usage=BudgetAmounts(calls=1),
        detail="stub-debate",
    )


@activity.defn
async def run_rag(payload: CostlyPathInput) -> CostlyPathResult:
    """Default retrieval stub. Real RAG scheduling is DP-33."""

    activity.logger.info("stub run_rag story=%s", payload.story_id)
    return CostlyPathResult(
        path="rag",
        has_new_evidence=True,
        usage=BudgetAmounts(calls=1),
        detail="stub-rag",
    )


@activity.defn
async def run_model_call(payload: CostlyPathInput) -> CostlyPathResult:
    """Default multi-model stub. Real model scheduling is out of scope."""

    activity.logger.info("stub run_model_call story=%s", payload.story_id)
    return CostlyPathResult(
        path="model",
        has_new_evidence=True,
        usage=BudgetAmounts(calls=1),
        detail="stub-model",
    )
