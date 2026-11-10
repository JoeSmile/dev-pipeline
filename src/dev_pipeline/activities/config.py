"""Activity that validates config-root. Filesystem I/O stays out of workflows."""

from temporalio import activity

from dev_pipeline.config_root import validate_config_root_path


@activity.defn
async def validate_config_root(config_root: str) -> str:
    """Fail visibly when config-root is missing or invalid; never fall back."""

    return validate_config_root_path(config_root)
