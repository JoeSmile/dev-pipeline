"""Operator-visible errors for illegal stage moves, config, start, and publish targets."""


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


class SpikeConfigError(ValueError):
    """Optional spike.toml is present but invalid; the spike must not continue."""


class WorkspaceConfigError(ValueError):
    """Optional workspace.toml is present but invalid; the sandbox must not continue."""


class SandboxViolation(ValueError):
    """A coding-sandbox write or command violated the authorization boundary."""

    def __init__(self, reason: str, path: str = "", authorized_worktree: str = "") -> None:
        self.reason = reason
        self.path = path
        self.authorized_worktree = authorized_worktree
        super().__init__(reason)


class ProductionDeployForbidden(ValueError):
    """Any production (or non-test) publish target is rejected."""


class PipelineStartError(ValueError):
    """CLI or client start failed visibly and must abort."""


class JiraConfigError(ValueError):
    """The jira.toml or token file is missing or invalid; mirror must not succeed."""


class JiraMirrorError(RuntimeError):
    """A Jira HTTP call failed; the Workflow stage must stay unchanged."""
