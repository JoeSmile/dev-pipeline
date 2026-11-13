"""Filesystem writes for Story Retro and human promote_rule. Activity-only."""

from __future__ import annotations

from pathlib import Path

from dev_pipeline.evidence_contract import read_text_utf8, write_text_utf8
from dev_pipeline.retro import (
    CANDIDATE_STATUS,
    PROMOTION_TARGETS,
    REASON_AUTO_PROMOTE,
    REASON_INELIGIBLE,
    REASON_UNKNOWN_CANDIDATE,
    CandidateRecord,
    PromoteApplyInput,
    PromoteApplyResult,
    PromotionRecord,
    RetroInput,
    RetroRecord,
    RetroResult,
    apply_human_promotion,
    collect_story_observations,
    daily_note_relpath,
    merge_observations,
    parse_candidate_text,
    reject_automatic_promotion,
    render_candidate_text,
    render_daily_note,
)
from dev_pipeline.workspace_config import resolve_knowledge_root

_SECRET_MARKERS = ("CURSOR_API_KEY", "Authorization:", "api_key=", "token=")
_SECRET_REDACTED = "（已省略可能的密钥）"


def knowledge_paths(config_root: str) -> tuple[Path, Path, Path]:
    """Return (knowledge_root, daily_dir, candidate_dir) for the configured root."""

    root = Path(resolve_knowledge_root(config_root))
    return root, root / "docs" / "knowledge" / "daily", root / "docs" / "knowledge" / "candidates"


def load_candidates(candidate_dir: Path) -> dict[str, CandidateRecord]:
    """Load existing candidate markdown files. Missing directory is empty."""

    records: dict[str, CandidateRecord] = {}
    if not candidate_dir.is_dir():
        return records
    for path in sorted(candidate_dir.glob("*.md")):
        text = read_text_utf8(path)
        record = parse_candidate_text(text, candidate_id=path.stem)
        if record.candidate_id:
            records[record.candidate_id] = record
    return records


def write_candidate(candidate_dir: Path, record: CandidateRecord) -> Path:
    """Write one UTF-8 candidate file. Parents are created as needed."""

    candidate_dir.mkdir(parents=True, exist_ok=True)
    path = candidate_dir / f"{record.candidate_id}.md"
    write_text_utf8(path, render_candidate_text(record))
    return path


def _scrub(text: str) -> str:
    for marker in _SECRET_MARKERS:
        if marker in text:
            return text.replace(marker, _SECRET_REDACTED)
    return text


def produce_retro_result(payload: RetroInput) -> RetroResult:
    """Append today's daily note and update candidates. Never auto-promotes."""

    root, daily_dir, candidate_dir = knowledge_paths(payload.config_root)
    existing_records = load_candidates(candidate_dir)
    observations = collect_story_observations(payload)
    candidates = merge_observations(existing_records, observations)
    for item in candidates:
        if item.status in PROMOTION_TARGETS and item.candidate_id not in existing_records:
            raise ValueError(REASON_AUTO_PROMOTE)
        previous = existing_records.get(item.candidate_id)
        if (
            item.status in PROMOTION_TARGETS
            and previous is not None
            and previous.status not in PROMOTION_TARGETS
        ):
            raise ValueError(REASON_AUTO_PROMOTE)
        write_candidate(candidate_dir, item)
    daily_dir.mkdir(parents=True, exist_ok=True)
    daily_path = root / daily_note_relpath(payload.daily_date)
    existing = read_text_utf8(daily_path) if daily_path.is_file() else ""
    text = _scrub(render_daily_note(existing, payload, candidates))
    write_text_utf8(daily_path, text)
    record = RetroRecord(
        daily_path=str(daily_path),
        daily_date=payload.daily_date,
        written_at=payload.written_at,
        candidates=candidates,
        detail="story-retro-written",
    )
    return RetroResult(recorded=True, detail=record.daily_path, record=record)


def produce_promote_result(payload: PromoteApplyInput) -> PromoteApplyResult:
    """Apply a human promotion on disk, or reject without writing a promotion."""

    request = payload.request
    if request.automatic:
        return PromoteApplyResult(promoted=False, reason=reject_automatic_promotion())
    _root, _daily_dir, candidate_dir = knowledge_paths(payload.config_root)
    existing = load_candidates(candidate_dir)
    record = existing.get(request.candidate_id)
    if record is None:
        return PromoteApplyResult(promoted=False, reason=REASON_UNKNOWN_CANDIDATE)
    updated, reason = apply_human_promotion(record, request)
    if updated is None:
        return PromoteApplyResult(
            promoted=False, reason=reason or REASON_INELIGIBLE, candidate=record
        )
    path = write_candidate(candidate_dir, updated)
    promotion = PromotionRecord(
        candidate_id=updated.candidate_id,
        target=updated.status,
        reason=updated.promotion_reason,
        evidence_citation=updated.promotion_citation,
        decided_at=updated.promoted_at,
        path=str(path),
    )
    return PromoteApplyResult(promoted=True, reason="", record=promotion, candidate=updated)


def refuse_auto_promote_in_retro(records: list[CandidateRecord], target: str) -> None:
    """Reject an attempt to promote candidates from the Retro write itself."""

    del records
    if target in PROMOTION_TARGETS or target:
        raise ValueError(REASON_AUTO_PROMOTE)


def new_candidate(candidate_id: str, title: str = "") -> CandidateRecord:
    """Return a fresh, unpromoted candidate record."""

    return CandidateRecord(
        candidate_id=candidate_id,
        title=title or candidate_id,
        status=CANDIDATE_STATUS,
    )
