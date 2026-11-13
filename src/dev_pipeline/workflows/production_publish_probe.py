"""Test-only parent-like probe for a rejected production publish.

Test-only workflow, not registered by the production worker. Kept next
to official workflows so the Temporal sandbox can import it safely.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

from dev_pipeline.models import PublishRequest, PublishResult

_NO_RETRY = RetryPolicy(maximum_attempts=1)

with workflow.unsafe.imports_passed_through():
    from dev_pipeline.activities.stubs import publish_to_test_env


@workflow.defn
class ProductionPublishProbe:
    """Execute publish_to_test_env so History can record a production rejection."""

    @workflow.run
    async def run(self, request: PublishRequest) -> PublishResult:
        result: PublishResult = await workflow.execute_activity(
            publish_to_test_env,
            request,
            start_to_close_timeout=timedelta(seconds=10),
            retry_policy=_NO_RETRY,
        )
        return result
