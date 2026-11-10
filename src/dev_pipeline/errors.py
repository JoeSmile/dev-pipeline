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


class ProductionDeployForbidden(ValueError):
    """Any production (or non-test) publish target is rejected."""


class UnhandledTicketFailure(Exception):
    """A required ticket failed and has not been retried, dropped, or aborted."""

    def __init__(self, ticket_id: str, reason: str = "") -> None:
        self.ticket_id = ticket_id
        message = f"必做 Ticket {ticket_id} 失败且尚未处理（重试、从清单中移除，或 abort）"
        if reason:
            message = f"{message}：{reason}"
        super().__init__(message)
