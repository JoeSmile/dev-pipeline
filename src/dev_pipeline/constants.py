"""Process-wide constants that do not depend on configuration or I/O."""

from datetime import timedelta
from typing import Literal

# Official already-running agent-platform Temporal on the Windows dev host.
# Do not start a second server on this port from this repository.
DEFAULT_TEMPORAL_TARGET = "127.0.0.1:7233"
OFFICIAL_TEMPORAL_CONTAINER = "agent-platform-temporal-1"
DEFAULT_TASK_QUEUE = "dev-pipeline"
TEST_PUBLISH_TARGET: Literal["test"] = "test"
ALLOWED_PUBLISH_TARGETS = frozenset({TEST_PUBLISH_TARGET})
CLIENT_CONNECT_TIMEOUT = timedelta(seconds=10)
WAITING_AUTHORIZE_TEST_DEPLOY = "正在等候你确认测试环境发布"
