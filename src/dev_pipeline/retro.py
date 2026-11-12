"""Story Retro types and pure helpers. Workflow-safe: no I/O or datetime.now()."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone

PROMOTION_TARGET_L0 = "L0"
PROMOTION_TARGET_GLOBAL_SKILL = "全局 Skill"
PROMOTION_TARGETS = frozenset({PROMOTION_TARGET_L0, PROMOTION_TARGET_GLOBAL_SKILL})
CANDIDATE_STATUS = "candidate"
MIN_HIT_COUNT = 2
DAILY_TZ_NAME = "Asia/Shanghai"
# Fixed UTC+8 offset. Asia/Shanghai has no DST; avoid zoneinfo so Workflow
# sandbox imports stay deterministic and do not receive a RestrictedProxy.
DAILY_TZ = timezone(timedelta(hours=8), name=DAILY_TZ_NAME)
DAILY_RELDIR = "docs/knowledge/daily"
CANDIDATE_RELDIR = "docs/knowledge/candidates"

REASON_AUTO_PROMOTE = "禁止自动升级到 L0 或全局 Skill，必须人工 Signal promote_rule"
REASON_INELIGIBLE = "未达到晋升条件（真实命中 N≥2 或一次硬近失），不能晋升"
REASON_UNKNOWN_CANDIDATE = "未知候选，不能晋升"
REASON_BAD_TARGET = "promote_rule 的 target 必须是 L0 或 全局 Skill"
REASON_MISSING_CITATION = "promote_rule 缺少证据引用，不能晋升"
DIFFICULTY_SCORE_MARKERS = ("技术难度", "业务难度")
CITATION_TEST_DEPLOY = "snapshot.test_deploy_address"
CITATION_SCHEME = "snapshot.scheme"
PENDING_CLAIM = "待证"


@dataclass
class RetroEvidenceLine:
    """One citable 红线 ↔ 证据 item carried into Story Retro."""

    red_line: str = ""
    result: str = ""
    evidence_path: str = ""
    command_or_phenomenon: str = ""


@dataclass
class RetroTicketEvidence:
    """Ticket facts the Retro activity may cite. Contains no secrets."""

    ticket_id: str
    stage: str
    dropped: bool = False
    verify_passed: bool = False
    verify_evidence_path: str = ""
    verify_lines: list[RetroEvidenceLine] = field(default_factory=list)
    requires_human_gate: bool = False


@dataclass
class RetroHypothesis:
    """Scheme hypothesis the Retro activity may record as a candidate."""

    statement: str
    grade: str = ""


@dataclass
class RetroInput:
    """Activity input for writing the Story Retro. Contains no secrets."""

    story_id: str
    execution_id: str
    config_root: str
    daily_date: str
    written_at: str = ""
    test_deploy_address: str = ""
    test_deploy_status: str = ""
    summary: str = ""
    tickets: list[RetroTicketEvidence] = field(default_factory=list)
    hypotheses: list[RetroHypothesis] = field(default_factory=list)


@dataclass
class CandidateRecord:
    """One knowledge candidate. Promotion status stays candidate until promote_rule."""

    candidate_id: str
    title: str = ""
    status: str = CANDIDATE_STATUS
    hits: int = 0
    hard_near_misses: int = 0
    citations: list[str] = field(default_factory=list)
    promotion_target: str = ""
    promotion_reason: str = ""
    promotion_citation: str = ""
    promoted_at: str = ""
    promoted_by: str = ""


@dataclass
class PromotionRecord:
    """Queryable record of a legal promote_rule that actually took effect."""

    candidate_id: str
    target: str
    reason: str
    evidence_citation: str
    decided_at: str = ""
    path: str = ""


@dataclass
class RetroRecord:
    """Queryable Story Retro result on the existing parent snapshot."""

    daily_path: str = ""
    daily_date: str = ""
    written_at: str = ""
    candidates: list[CandidateRecord] = field(default_factory=list)
    claims_pending: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass
class RetroResult:
    """Activity result after appending the daily note and updating candidates."""

    recorded: bool = False
    detail: str = ""
    record: RetroRecord | None = None


@dataclass
class PromoteRuleRequest:
    """Human promote_rule payload. Automatic promotion is always rejected.

    Required fields: 候选标识, 晋升目标 (L0 / 全局 Skill), 理由, 证据引用.
    ``decided_at`` is stamped by the workflow from ``workflow.now()``;
    any client-supplied timestamp is ignored.
    """

    candidate_id: str
    target: str
    reason: str
    evidence_citation: str
    automatic: bool = False
    decided_at: str = ""


@dataclass
class PromoteApplyInput:
    """Activity input for applying a validated, non-automatic promote_rule."""

    request: PromoteRuleRequest
    config_root: str


@dataclass
class PromoteApplyResult:
    """Activity result for promote_rule. A rejection must not change files."""

    promoted: bool = False
    reason: str = ""
    record: PromotionRecord | None = None
    candidate: CandidateRecord | None = None


def daily_note_date(moment: datetime) -> str:
    """Return the calendar date for a daily note in Asia/Shanghai.

    The instant must come from ``workflow.now()`` (or a test double). This
    helper does not read the OS local timezone and does not call datetime.now().
    Asia/Shanghai is represented as a fixed UTC+8 offset (no DST).
    """

    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(DAILY_TZ).date().isoformat()


def daily_note_relpath(daily_date: str) -> str:
    """Return the repo-relative path of that day's daily note."""

    return f"{DAILY_RELDIR}/{daily_date}.md"


def candidate_relpath(candidate_id: str) -> str:
    """Return the repo-relative path of one candidate record."""

    return f"{CANDIDATE_RELDIR}/{candidate_id}.md"


def is_eligible_for_promotion(record: CandidateRecord) -> bool:
    """Return True when the default upgrade conditions are met.

    Default (may be re-decided later when confirming L0; do not silently change):
    real hit N≥2, or one hard near-miss. A human ``promote_rule`` is still required.
    """

    return record.hits >= MIN_HIT_COUNT or record.hard_near_misses >= 1


def normalize_promotion_target(value: object) -> str | None:
    """Return a legal promotion target, or None when the value is not allowed."""

    if not isinstance(value, str):
        return None
    text = " ".join(value.strip().split())
    lowered = text.casefold().replace("_", " ").replace("-", " ")
    if text == PROMOTION_TARGET_L0 or lowered == "l0":
        return PROMOTION_TARGET_L0
    if text == PROMOTION_TARGET_GLOBAL_SKILL or lowered in {
        "全局 skill",
        "全局skill",
        "global skill",
    }:
        return PROMOTION_TARGET_GLOBAL_SKILL
    return None


def reject_automatic_promotion(target: str = "") -> str:
    """Return the operator-visible reason that automatic promotion is forbidden."""

    del target
    return REASON_AUTO_PROMOTE


def apply_candidate_observation(
    record: CandidateRecord,
    *,
    hits_delta: int = 0,
    hard_near_misses_delta: int = 0,
    citation: str = "",
) -> CandidateRecord:
    """Record a hit or hard near-miss. Never changes status to L0 / 全局 Skill."""

    citations = list(record.citations)
    text = citation.strip()
    if text and text not in citations:
        citations.append(text)
    return CandidateRecord(
        candidate_id=record.candidate_id,
        title=record.title,
        status=CANDIDATE_STATUS if record.status in {CANDIDATE_STATUS, ""} else record.status,
        hits=max(0, record.hits + hits_delta),
        hard_near_misses=max(0, record.hard_near_misses + hard_near_misses_delta),
        citations=citations,
        promotion_target=record.promotion_target,
        promotion_reason=record.promotion_reason,
        promotion_citation=record.promotion_citation,
        promoted_at=record.promoted_at,
        promoted_by=record.promoted_by,
    )


def apply_human_promotion(
    record: CandidateRecord,
    request: PromoteRuleRequest,
) -> tuple[CandidateRecord | None, str | None]:
    """Apply a human promotion, or return a rejection reason without changing status."""

    if request.automatic:
        return None, reject_automatic_promotion(request.target)
    target = normalize_promotion_target(request.target)
    if target is None:
        return None, REASON_BAD_TARGET
    if not request.evidence_citation.strip():
        return None, REASON_MISSING_CITATION
    if not is_eligible_for_promotion(record):
        return None, REASON_INELIGIBLE
    updated = CandidateRecord(
        candidate_id=record.candidate_id,
        title=record.title or record.candidate_id,
        status=target,
        hits=record.hits,
        hard_near_misses=record.hard_near_misses,
        citations=list(record.citations),
        promotion_target=target,
        promotion_reason=request.reason.strip(),
        promotion_citation=request.evidence_citation.strip(),
        promoted_at=request.decided_at,
        promoted_by="promote_rule",
    )
    citation = request.evidence_citation.strip()
    if citation and citation not in updated.citations:
        updated.citations.append(citation)
    return updated, None


def candidate_id_for(kind: str, key: str) -> str:
    """Return a stable, filesystem-safe candidate id for a statement or ticket."""

    seed = f"{kind}:{key.strip()}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    return f"{kind}-{digest}"


def is_difficulty_score_report(text: str) -> bool:
    """Return True when a note looks like a 技术难度 / 业务难度 score report."""

    return any(marker in text for marker in DIFFICULTY_SCORE_MARKERS)


def render_candidate_text(record: CandidateRecord) -> str:
    """Render a UTF-8 candidate record. Status is never silently upgraded here."""

    citation_lines = [f"- {item}" for item in record.citations] or ["- （无）"]
    eligible = "是" if is_eligible_for_promotion(record) else "否"
    lines = [
        f"# Candidate: {record.candidate_id}",
        "",
        f"- 标题: {record.title or record.candidate_id}",
        f"- 状态: {record.status or CANDIDATE_STATUS}",
        f"- 命中次数: {record.hits}",
        f"- 硬近失: {record.hard_near_misses}",
        f"- 可晋升: {eligible}",
        "- 证据:",
        *citation_lines,
    ]
    if record.promotion_target:
        lines.extend(
            [
                "- 晋升:",
                f"  - 目标: {record.promotion_target}",
                f"  - 理由: {record.promotion_reason}",
                f"  - 证据引用: {record.promotion_citation}",
                f"  - 时间: {record.promoted_at}",
            ]
        )
    return "\n".join(lines) + "\n"


def parse_candidate_text(text: str, *, candidate_id: str = "") -> CandidateRecord:
    """Parse a candidate markdown record. Unknown keys are ignored."""

    fields: dict[str, str] = {}
    citations: list[str] = []
    in_citations = False
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("# Candidate:"):
            fields["id"] = line.split(":", 1)[1].strip()
            in_citations = False
            continue
        if line == "- 证据:":
            in_citations = True
            continue
        if line == "- 晋升:":
            in_citations = False
            continue
        if in_citations and line.startswith("- ") and not line.startswith("- 证据"):
            item = line[2:].strip()
            if item and item != "（无）":
                citations.append(item)
            continue
        if line.startswith("- 标题:"):
            fields["title"] = line.split(":", 1)[1].strip()
            in_citations = False
        elif line.startswith("- 状态:"):
            fields["status"] = line.split(":", 1)[1].strip()
            in_citations = False
        elif line.startswith("- 命中次数:"):
            fields["hits"] = line.split(":", 1)[1].strip()
            in_citations = False
        elif line.startswith("- 硬近失:"):
            fields["near"] = line.split(":", 1)[1].strip()
            in_citations = False
        elif line.startswith("- 目标:") or line.startswith("  - 目标:"):
            fields["target"] = line.split(":", 1)[1].strip()
        elif line.startswith("- 理由:") or line.startswith("  - 理由:"):
            fields["reason"] = line.split(":", 1)[1].strip()
        elif line.startswith("- 证据引用:") or line.startswith("  - 证据引用:"):
            fields["promo_cite"] = line.split(":", 1)[1].strip()
        elif line.startswith("- 时间:") or line.startswith("  - 时间:"):
            fields["promoted_at"] = line.split(":", 1)[1].strip()
    ident = fields.get("id") or candidate_id
    return CandidateRecord(
        candidate_id=ident,
        title=fields.get("title") or ident,
        status=fields.get("status") or CANDIDATE_STATUS,
        hits=_as_int(fields.get("hits")),
        hard_near_misses=_as_int(fields.get("near")),
        citations=citations,
        promotion_target=fields.get("target") or "",
        promotion_reason=fields.get("reason") or "",
        promotion_citation=fields.get("promo_cite") or "",
        promoted_at=fields.get("promoted_at") or "",
        promoted_by="promote_rule" if fields.get("target") else "",
    )


def _as_int(value: str | None) -> int:
    if value is None:
        return 0
    try:
        return int(value.strip())
    except ValueError:
        return 0


def collect_story_observations(
    payload: RetroInput,
) -> list[tuple[CandidateRecord, str]]:
    """Derive candidate hit / near-miss updates from this Story's evidence."""

    from dev_pipeline.verify import RESULT_HELD

    observations: list[tuple[CandidateRecord, str]] = []
    for ticket in payload.tickets:
        if ticket.dropped or ticket.stage != "Verified":
            continue
        for line in ticket.verify_lines:
            if line.result != RESULT_HELD or not line.red_line.strip():
                continue
            citation = line.evidence_path.strip() or ticket.verify_evidence_path.strip()
            ident = candidate_id_for("red-line", line.red_line)
            seed = CandidateRecord(candidate_id=ident, title=line.red_line.strip())
            observations.append((seed, "hit:" + citation))
        if ticket.requires_human_gate:
            ident = candidate_id_for("near-miss", ticket.ticket_id)
            citation = ticket.verify_evidence_path.strip() or f"tickets[].review:{ticket.ticket_id}"
            title = f"Ticket {ticket.ticket_id} 经 HumanGate 后仍验证过线"
            seed = CandidateRecord(candidate_id=ident, title=title)
            observations.append((seed, "near:" + citation))
    for hypothesis in payload.hypotheses:
        if hypothesis.grade != "候选" or not hypothesis.statement.strip():
            continue
        ident = candidate_id_for("hypothesis", hypothesis.statement)
        seed = CandidateRecord(candidate_id=ident, title=hypothesis.statement.strip())
        observations.append((seed, "hit:" + CITATION_SCHEME))
    return observations


def merge_observations(
    existing: dict[str, CandidateRecord],
    observations: list[tuple[CandidateRecord, str]],
) -> list[CandidateRecord]:
    """Apply hits / near-misses onto existing records without promoting."""

    updated = dict(existing)
    for seed, tag in observations:
        current = updated.get(seed.candidate_id) or seed
        if current.title == current.candidate_id and seed.title:
            current = CandidateRecord(
                candidate_id=current.candidate_id,
                title=seed.title,
                status=current.status,
                hits=current.hits,
                hard_near_misses=current.hard_near_misses,
                citations=list(current.citations),
                promotion_target=current.promotion_target,
                promotion_reason=current.promotion_reason,
                promotion_citation=current.promotion_citation,
                promoted_at=current.promoted_at,
                promoted_by=current.promoted_by,
            )
        if tag.startswith("hit:"):
            current = apply_candidate_observation(current, hits_delta=1, citation=tag[4:])
        elif tag.startswith("near:"):
            current = apply_candidate_observation(
                current, hard_near_misses_delta=1, citation=tag[5:]
            )
        # Keep a previously promoted status if a human already signed it; Retro
        # itself never assigns L0 / 全局 Skill.
        if current.status in PROMOTION_TARGETS:
            current = CandidateRecord(
                candidate_id=current.candidate_id,
                title=current.title,
                status=current.status,
                hits=current.hits,
                hard_near_misses=current.hard_near_misses,
                citations=list(current.citations),
                promotion_target=current.promotion_target,
                promotion_reason=current.promotion_reason,
                promotion_citation=current.promotion_citation,
                promoted_at=current.promoted_at,
                promoted_by=current.promoted_by,
            )
        updated[current.candidate_id] = current
    return [updated[key] for key in sorted(updated)]


def render_story_section(payload: RetroInput, candidates: list[CandidateRecord]) -> str:
    """Render this Story's evidence section. Not a difficulty-score report."""

    lines = [
        f"## Story {payload.story_id}（execution {payload.execution_id}）",
        "",
        "本段是 Story Retro 证据纪要，不是个人日终复盘，也不是难度打分日报。",
        "",
    ]
    if payload.written_at:
        lines.append(f"- 写入时间: {payload.written_at}")
    address = payload.test_deploy_address.strip()
    if address:
        lines.append(f"- 测试部署: 成功，地址 {address}。证据：`{CITATION_TEST_DEPLOY}`")
    elif payload.test_deploy_status:
        lines.append(f"- 测试部署: {payload.test_deploy_status}。{PENDING_CLAIM}")
    for ticket in payload.tickets:
        if ticket.dropped:
            lines.append(f"- Ticket {ticket.ticket_id}: 已从清单移除，不再阻挡 Retro。")
            continue
        if ticket.stage != "Verified":
            lines.append(
                f"- Ticket {ticket.ticket_id}: 阶段 {ticket.stage}，未引用验证证据。{PENDING_CLAIM}"
            )
            continue
        held = [item for item in ticket.verify_lines if item.result == "守住" and item.red_line]
        citation = ticket.verify_evidence_path.strip()
        if held and citation:
            names = "；".join(item.red_line for item in held)
            lines.append(
                f"- Ticket {ticket.ticket_id}: 已验证过线。红线「{names}」守住。证据：`{citation}`"
            )
        elif held:
            names = "；".join(item.red_line for item in held)
            lines.append(
                f"- Ticket {ticket.ticket_id}: 已验证过线。红线「{names}」守住。{PENDING_CLAIM}"
            )
        elif citation:
            lines.append(f"- Ticket {ticket.ticket_id}: 已验证过线。证据：`{citation}`")
        else:
            lines.append(
                f"- Ticket {ticket.ticket_id}: 已验证过线，但缺少证据路径。{PENDING_CLAIM}"
            )
    if candidates:
        lines.append("")
        lines.append("### Candidates")
        for item in candidates:
            lines.append(
                f"- `{item.candidate_id}`（{item.title}）：命中 {item.hits}，"
                f"硬近失 {item.hard_near_misses}，状态 {item.status}。"
                "未自动晋升（晋升须人工 promote_rule）。"
                + (f" 证据：`{item.citations[-1]}`" if item.citations else f" {PENDING_CLAIM}")
            )
    return "\n".join(lines).rstrip() + "\n"


def render_daily_note(existing: str, payload: RetroInput, candidates: list[CandidateRecord]) -> str:
    """Create or append today's daily note without wiping earlier stories."""

    section = render_story_section(payload, candidates)
    if is_difficulty_score_report(section):
        raise ValueError("Story Retro 不得写成技术难度 / 业务难度打分日报")
    if existing.strip():
        return existing.rstrip() + "\n\n" + section
    return f"# {payload.daily_date} 日记\n\n{section}"
