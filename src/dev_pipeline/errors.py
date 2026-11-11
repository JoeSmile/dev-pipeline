"""Operator-visible errors for illegal stage moves, config-root, and publish targets."""


class IllegalTransition(Exception):
    """A requested stage move is not on the legal path."""

    def __init__(
        self,
        source: str,
        dest: str,
        trigger: str,
        reason: str = "",
    ) -> None:
        self.source = source
        self.dest = dest
        self.trigger = trigger
        self.reason = reason
        message = f"非法转移: {source} --{trigger}--> {dest}"
        if reason:
            message = f"{message}。{reason}"
        super().__init__(message)


class ConfigRootError(ValueError):
    """The required config-root is missing or invalid; execution must not continue."""


class BudgetConfigError(ValueError):
    """The required budget.toml is missing or invalid; execution must not continue."""


class ProductionDeployForbidden(ValueError):
    """Any production (or non-test) publish target is rejected."""
