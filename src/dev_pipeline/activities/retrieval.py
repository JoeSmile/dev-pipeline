"""Scheme-phase retrieval Activity. I/O stays here, not in the Workflow body."""

from temporalio import activity

from dev_pipeline.retrieval import SchemeRetrievalInput, SchemeRetrievalResult
from dev_pipeline.retrieval_backend import retrieve_scheme_knowledge_from_path


@activity.defn
async def retrieve_scheme_knowledge(payload: SchemeRetrievalInput) -> SchemeRetrievalResult:
    """Search the authorized scheme corpus and return an honest outcome.

    Failure, no-hit, and unconfigured-stub paths do not claim 已检索到.
    """

    activity.logger.info(
        "retrieve_scheme_knowledge story=%s execution=%s",
        payload.story_id,
        payload.execution_id,
    )
    return retrieve_scheme_knowledge_from_path(payload)
