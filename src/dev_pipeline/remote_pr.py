"""In-process remote-PR detection used by abort cleanup.

Default is that this repo never opens a remote PR. Tests may register a
fake existing PR. Close and merge helpers exist only so tests can prove
the recycle path never calls them. This module performs no network I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from dev_pipeline.abort_record import RemotePrHumanPending


@dataclass
class RemotePrRef:
    """A detected existing remote PR. Not a production marking scheme."""

    branch_name: str
    pr_ref: str
    ticket_id: str = ""


@dataclass
class RecordingPrClient:
    """Test double that records close/merge attempts and known open PRs."""

    open_prs: list[RemotePrRef] = field(default_factory=list)
    close_calls: list[str] = field(default_factory=list)
    merge_calls: list[str] = field(default_factory=list)

    def find_open_prs(self, branches: list[str]) -> list[RemotePrRef]:
        """Return registered open PRs whose branch is in *branches*."""

        wanted = set(branches)
        return [item for item in self.open_prs if item.branch_name in wanted]

    def close_pr(self, pr: RemotePrRef) -> None:
        """Record a close attempt. Abort cleanup must never call this."""

        self.close_calls.append(pr.pr_ref)

    def merge_pr(self, pr: RemotePrRef) -> None:
        """Record a merge attempt. Abort cleanup must never call this."""

        self.merge_calls.append(pr.pr_ref)


_DEFAULT_CLIENT: RecordingPrClient | None = None


def default_pr_client() -> RecordingPrClient:
    """Return the process-wide PR client used by the abort cleanup activity."""

    global _DEFAULT_CLIENT
    if _DEFAULT_CLIENT is None:
        _DEFAULT_CLIENT = RecordingPrClient()
    return _DEFAULT_CLIENT


def reset_default_pr_client() -> None:
    """Clear registered fake PRs and recorded calls. Intended for tests."""

    default_pr_client().open_prs.clear()
    default_pr_client().close_calls.clear()
    default_pr_client().merge_calls.clear()


def register_fake_remote_pr(
    branch_name: str,
    pr_ref: str,
    *,
    ticket_id: str = "",
) -> RemotePrRef:
    """Register a pre-existing remote PR for abort-cleanup tests."""

    item = RemotePrRef(branch_name=branch_name, pr_ref=pr_ref, ticket_id=ticket_id)
    default_pr_client().open_prs.append(item)
    return item


def pending_from_ref(pr: RemotePrRef) -> RemotePrHumanPending:
    """Build the snapshot record that a human must handle this PR."""

    return RemotePrHumanPending(
        ticket_id=pr.ticket_id,
        branch_name=pr.branch_name,
        pr_ref=pr.pr_ref,
    )
