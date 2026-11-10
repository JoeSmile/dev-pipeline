"""Process-wide constants that do not depend on configuration or I/O."""

DEFAULT_TEMPORAL_TARGET = "localhost:7233"
DEFAULT_TASK_QUEUE = "dev-pipeline"
ALLOWED_PUBLISH_TARGETS = frozenset({"test"})
