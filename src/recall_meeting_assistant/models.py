"""Normalized data models for the Recall.ai meeting-assistant package.

These mirror the persisted objects described in
docs/architecture.md (meeting sessions,
participants, transcript segments, action items).  Each model validates its
required fields and round-trips through ``to_dict`` / ``from_dict`` so storage
and webhook layers can serialize without bespoke glue.

Meeting URLs are sensitive (they grant call access), so sessions store a
redacted form via :func:`redact_meeting_url` rather than the raw URL.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

EXTERNAL_PROVIDER = "recall_ai"


# ── datetime / dict helpers ─────────────────────────────────────────────────
def _parse_datetime(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _serialize_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value.astimezone(timezone.utc)
    return normalized.isoformat().replace("+00:00", "Z")


def _clean_dict(values: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in values.items() if value is not None}


def redact_meeting_url(url: str | None) -> str | None:
    """Return a log-safe form of a meeting URL.

    Keeps the scheme + host (useful for routing/domain checks) but drops the
    meeting code path and any query/fragment, which are the access-granting
    parts.  Examples:

        https://meet.google.com/abc-defg-hij  →  https://meet.google.com/***
        meet.google.com/abc-defg-hij          →  meet.google.com/***
    """
    if not url:
        return None
    text = str(url).strip()
    if not text:
        return None
    parsed = urlparse(text if "//" in text else f"//{text}", scheme="https")
    host = parsed.netloc or parsed.path.split("/", 1)[0]
    if not host:
        return "***"
    scheme = parsed.scheme or "https"
    return f"{scheme}://{host}/***"


# ── meeting_sessions ────────────────────────────────────────────────────────
@dataclass
class MeetingSession:
    id: str
    recall_bot_id: str | None = None
    recall_recording_id: str | None = None
    recall_transcript_id: str | None = None
    meeting_url_redacted: str | None = None
    title: str | None = None
    external_provider: str = EXTERNAL_PROVIDER
    started_at: datetime | None = None
    ended_at: datetime | None = None
    status: str = "created"
    transcript_status: str = "pending"
    summary_status: str = "pending"
    fallback_used: bool = False
    fallback_reason: str | None = None
    telegram_delivery_status: str = "pending"
    artifact_dir: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.id).strip():
            raise ValueError("MeetingSession.id is required.")
        self.fallback_used = bool(self.fallback_used)
        self.started_at = _parse_datetime(self.started_at)
        self.ended_at = _parse_datetime(self.ended_at)
        self.created_at = _parse_datetime(self.created_at)
        self.updated_at = _parse_datetime(self.updated_at)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MeetingSession":
        return cls(
            id=str(payload.get("id") or "").strip(),
            recall_bot_id=payload.get("recall_bot_id"),
            recall_recording_id=payload.get("recall_recording_id"),
            recall_transcript_id=payload.get("recall_transcript_id"),
            meeting_url_redacted=payload.get("meeting_url_redacted"),
            title=payload.get("title"),
            external_provider=payload.get("external_provider") or EXTERNAL_PROVIDER,
            started_at=payload.get("started_at"),
            ended_at=payload.get("ended_at"),
            status=payload.get("status") or "created",
            transcript_status=payload.get("transcript_status") or "pending",
            summary_status=payload.get("summary_status") or "pending",
            fallback_used=payload.get("fallback_used", False),
            fallback_reason=payload.get("fallback_reason"),
            telegram_delivery_status=payload.get("telegram_delivery_status") or "pending",
            artifact_dir=payload.get("artifact_dir"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "id": self.id,
                "recall_bot_id": self.recall_bot_id,
                "recall_recording_id": self.recall_recording_id,
                "recall_transcript_id": self.recall_transcript_id,
                "meeting_url_redacted": self.meeting_url_redacted,
                "title": self.title,
                "external_provider": self.external_provider,
                "started_at": _serialize_datetime(self.started_at),
                "ended_at": _serialize_datetime(self.ended_at),
                "status": self.status,
                "transcript_status": self.transcript_status,
                "summary_status": self.summary_status,
                "fallback_used": self.fallback_used,
                "fallback_reason": self.fallback_reason,
                "telegram_delivery_status": self.telegram_delivery_status,
                "artifact_dir": self.artifact_dir,
                "created_at": _serialize_datetime(self.created_at),
                "updated_at": _serialize_datetime(self.updated_at),
            }
        )


# ── meeting_participants ────────────────────────────────────────────────────
@dataclass
class MeetingParticipant:
    meeting_id: str
    raw_display_name: str | None = None
    recall_participant_id: str | None = None
    email: str | None = None
    canonical_name: str | None = None
    telegram_username: str | None = None
    telegram_user_id: str | None = None
    match_confidence: float | None = None
    match_reason: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("MeetingParticipant.meeting_id is required.")
        if self.match_confidence is not None:
            self.match_confidence = float(self.match_confidence)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MeetingParticipant":
        return cls(
            meeting_id=str(payload.get("meeting_id") or "").strip(),
            raw_display_name=payload.get("raw_display_name"),
            recall_participant_id=payload.get("recall_participant_id"),
            email=payload.get("email"),
            canonical_name=payload.get("canonical_name"),
            telegram_username=payload.get("telegram_username"),
            telegram_user_id=payload.get("telegram_user_id"),
            match_confidence=payload.get("match_confidence"),
            match_reason=payload.get("match_reason"),
            id=payload.get("id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "id": self.id,
                "meeting_id": self.meeting_id,
                "recall_participant_id": self.recall_participant_id,
                "raw_display_name": self.raw_display_name,
                "email": self.email,
                "canonical_name": self.canonical_name,
                "telegram_username": self.telegram_username,
                "telegram_user_id": self.telegram_user_id,
                "match_confidence": self.match_confidence,
                "match_reason": self.match_reason,
            }
        )


# ── transcript_segments ─────────────────────────────────────────────────────
@dataclass
class TranscriptSegment:
    meeting_id: str
    segment_index: int
    start_ms: int
    end_ms: int
    text: str
    speaker_raw: str | None = None
    speaker_canonical: str | None = None
    provider: str | None = None
    confidence: float | None = None
    raw_json: dict[str, Any] | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("TranscriptSegment.meeting_id is required.")
        self.segment_index = int(self.segment_index)
        self.start_ms = int(self.start_ms)
        self.end_ms = int(self.end_ms)
        if self.confidence is not None:
            self.confidence = float(self.confidence)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "TranscriptSegment":
        return cls(
            meeting_id=str(payload.get("meeting_id") or "").strip(),
            segment_index=payload.get("segment_index") or 0,
            start_ms=payload.get("start_ms") or 0,
            end_ms=payload.get("end_ms") or 0,
            text=str(payload.get("text") or ""),
            speaker_raw=payload.get("speaker_raw"),
            speaker_canonical=payload.get("speaker_canonical"),
            provider=payload.get("provider"),
            confidence=payload.get("confidence"),
            raw_json=payload.get("raw_json"),
            id=payload.get("id"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "id": self.id,
                "meeting_id": self.meeting_id,
                "segment_index": self.segment_index,
                "start_ms": self.start_ms,
                "end_ms": self.end_ms,
                "speaker_raw": self.speaker_raw,
                "speaker_canonical": self.speaker_canonical,
                "text": self.text,
                "provider": self.provider,
                "confidence": self.confidence,
                "raw_json": self.raw_json,
            }
        )


# ── meeting_action_items ────────────────────────────────────────────────────
@dataclass
class MeetingActionItem:
    meeting_id: str
    task: str
    owner_canonical: str = "unassigned"
    owner_telegram: str | None = None
    due_date: str | None = None
    priority: str | None = None
    status: str = "open"
    confidence: str | None = None
    evidence_segment_ids: list[str] = field(default_factory=list)
    id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("MeetingActionItem.meeting_id is required.")
        if not str(self.task).strip():
            raise ValueError("MeetingActionItem.task is required.")
        self.owner_canonical = self.owner_canonical or "unassigned"
        self.status = self.status or "open"
        self.evidence_segment_ids = [str(s) for s in (self.evidence_segment_ids or [])]
        self.created_at = _parse_datetime(self.created_at)
        self.updated_at = _parse_datetime(self.updated_at)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "MeetingActionItem":
        return cls(
            meeting_id=str(payload.get("meeting_id") or "").strip(),
            task=str(payload.get("task") or "").strip(),
            owner_canonical=payload.get("owner_canonical") or "unassigned",
            owner_telegram=payload.get("owner_telegram"),
            due_date=payload.get("due_date"),
            priority=payload.get("priority"),
            status=payload.get("status") or "open",
            confidence=payload.get("confidence"),
            evidence_segment_ids=list(payload.get("evidence_segment_ids") or []),
            id=payload.get("id"),
            created_at=payload.get("created_at"),
            updated_at=payload.get("updated_at"),
        )

    def to_dict(self) -> dict[str, Any]:
        return _clean_dict(
            {
                "id": self.id,
                "meeting_id": self.meeting_id,
                "owner_canonical": self.owner_canonical,
                "owner_telegram": self.owner_telegram,
                "task": self.task,
                "due_date": self.due_date,
                "priority": self.priority,
                "status": self.status,
                "confidence": self.confidence,
                "evidence_segment_ids": self.evidence_segment_ids or None,
                "created_at": _serialize_datetime(self.created_at),
                "updated_at": _serialize_datetime(self.updated_at),
            }
        )


__all__ = [
    "EXTERNAL_PROVIDER",
    "MeetingSession",
    "MeetingParticipant",
    "TranscriptSegment",
    "MeetingActionItem",
    "redact_meeting_url",
]
