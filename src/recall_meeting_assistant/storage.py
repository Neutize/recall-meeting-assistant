"""SQLite-backed storage for the Recall.ai meeting-assistant package.

Persists the objects described in the "Internal data model" section of
docs/architecture.md: meeting sessions,
participants, transcript segments, notes, action items, and Telegram delivery
records.  A single SQLite file (``meetings.db``) lives at the root of the
configured storage directory; generated artifacts (transcripts, audio) live in
per-meeting subdirectories of that same root.

Two invariants hold here:

1. **Artifacts stay inside the storage root.**  :meth:`MeetingStore.artifact_path`
   resolves every path under ``storage_dir`` and rejects absolute components or
   ``..`` traversal that would escape it (:class:`ArtifactPathError`).
2. **JSON-ish fields and timestamps round-trip safely.**  Lists / dicts
   (raw transcript JSON, evidence segment ids, message ids, summary blocks) are
   stored as JSON text; datetimes are stored as stable ISO-8601 UTC strings.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from recall_meeting_assistant.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingSession,
    TranscriptSegment,
)

if TYPE_CHECKING:
    from recall_meeting_assistant.config import RecallMeetingsConfig

DB_FILENAME = "meetings.db"


# ── helpers ──────────────────────────────────────────────────────────────────
def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    """Serialize a datetime as a stable ISO-8601 UTC string (``...Z``)."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _dump_json(value: Any) -> str | None:
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _load_json(value: Any, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    text = str(value).strip()
    if not text:
        return default
    return json.loads(text)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


# ── new persisted objects (not in models.py) ─────────────────────────────────
@dataclass
class MeetingNotes:
    """A generated summary for a meeting (``meeting_notes`` table)."""

    meeting_id: str
    executive_summary: list[str] = field(default_factory=list)
    executive_summary_markdown: str | None = None
    action_items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    model_provider: str | None = None
    model_name: str | None = None
    id: str | None = None
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("MeetingNotes.meeting_id is required.")
        self.created_at = _parse_iso(self.created_at)


@dataclass
class TelegramDelivery:
    """A record of one delivery attempt (``telegram_deliveries`` table)."""

    meeting_id: str
    backend: str
    chat_id: str | None = None
    thread_id: str | None = None
    message_ids: list[str] = field(default_factory=list)
    delivered_at: datetime | None = None
    error: str | None = None
    id: str | None = None

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("TelegramDelivery.meeting_id is required.")
        if not str(self.backend).strip():
            raise ValueError("TelegramDelivery.backend is required.")
        self.message_ids = [str(m) for m in (self.message_ids or [])]
        self.delivered_at = _parse_iso(self.delivered_at)


# ── errors ───────────────────────────────────────────────────────────────────
class ArtifactPathError(ValueError):
    """Raised when an artifact path would resolve outside the storage root."""


# ── schema ───────────────────────────────────────────────────────────────────
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_sessions (
    id TEXT PRIMARY KEY,
    recall_bot_id TEXT,
    recall_recording_id TEXT,
    recall_transcript_id TEXT,
    meeting_url_redacted TEXT,
    title TEXT,
    external_provider TEXT,
    started_at TEXT,
    ended_at TEXT,
    status TEXT,
    transcript_status TEXT,
    summary_status TEXT,
    fallback_used INTEGER NOT NULL DEFAULT 0,
    fallback_reason TEXT,
    telegram_delivery_status TEXT,
    artifact_dir TEXT,
    created_at TEXT,
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS meeting_participants (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    recall_participant_id TEXT,
    raw_display_name TEXT,
    email TEXT,
    canonical_name TEXT,
    telegram_username TEXT,
    telegram_user_id TEXT,
    match_confidence REAL,
    match_reason TEXT
);
CREATE INDEX IF NOT EXISTS ix_participants_meeting ON meeting_participants(meeting_id);

CREATE TABLE IF NOT EXISTS transcript_segments (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    segment_index INTEGER NOT NULL,
    start_ms INTEGER NOT NULL,
    end_ms INTEGER NOT NULL,
    speaker_raw TEXT,
    speaker_canonical TEXT,
    text TEXT NOT NULL,
    provider TEXT,
    confidence REAL,
    raw_json TEXT
);
CREATE INDEX IF NOT EXISTS ix_segments_meeting ON transcript_segments(meeting_id, segment_index);

CREATE TABLE IF NOT EXISTS meeting_notes (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    executive_summary_json TEXT,
    executive_summary_markdown TEXT,
    action_items_json TEXT,
    decisions_json TEXT,
    open_questions_json TEXT,
    risks_json TEXT,
    model_provider TEXT,
    model_name TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_notes_meeting ON meeting_notes(meeting_id, created_at);

CREATE TABLE IF NOT EXISTS meeting_action_items (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    owner_canonical TEXT,
    owner_telegram TEXT,
    task TEXT NOT NULL,
    due_date TEXT,
    priority TEXT,
    status TEXT,
    confidence TEXT,
    evidence_segment_ids TEXT,
    created_at TEXT,
    updated_at TEXT
);
CREATE INDEX IF NOT EXISTS ix_action_items_meeting ON meeting_action_items(meeting_id);

CREATE TABLE IF NOT EXISTS telegram_deliveries (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    backend TEXT NOT NULL,
    chat_id TEXT,
    thread_id TEXT,
    message_ids TEXT,
    delivered_at TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS ix_deliveries_meeting ON telegram_deliveries(meeting_id);
"""


# ── store ────────────────────────────────────────────────────────────────────
class MeetingStore:
    """Persistent store for meeting-assistant data, rooted at ``storage_dir``.

    The SQLite database lives at ``storage_dir / meetings.db``; per-meeting
    artifacts live under ``storage_dir / <meeting_id>/`` (see
    :meth:`artifact_path`).  Use as a context manager or call :meth:`close`.
    """

    def __init__(self, storage_dir: str | Path) -> None:
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        # Resolve once for containment checks (handles symlinked tmp dirs).
        self._root = self.storage_dir.resolve()
        self.connection = sqlite3.connect(str(self.storage_dir / DB_FILENAME))
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.executescript(_SCHEMA)
        self.connection.commit()

    @classmethod
    def from_config(cls, config: "RecallMeetingsConfig") -> "MeetingStore":
        return cls(config.storage_dir)

    # ── lifecycle ────────────────────────────────────────────────────────────
    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "MeetingStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ── meeting sessions ─────────────────────────────────────────────────────
    def create_session(self, session: MeetingSession) -> MeetingSession:
        """Insert (or replace) a meeting session, stamping created/updated."""
        now = _utcnow()
        if session.created_at is None:
            session.created_at = now
        if session.updated_at is None:
            session.updated_at = now
        self.connection.execute(
            """
            INSERT OR REPLACE INTO meeting_sessions (
                id, recall_bot_id, recall_recording_id, recall_transcript_id,
                meeting_url_redacted, title, external_provider, started_at,
                ended_at, status, transcript_status, summary_status,
                fallback_used, fallback_reason, telegram_delivery_status,
                artifact_dir, created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                session.id,
                session.recall_bot_id,
                session.recall_recording_id,
                session.recall_transcript_id,
                session.meeting_url_redacted,
                session.title,
                session.external_provider,
                _to_iso(session.started_at),
                _to_iso(session.ended_at),
                session.status,
                session.transcript_status,
                session.summary_status,
                1 if session.fallback_used else 0,
                session.fallback_reason,
                session.telegram_delivery_status,
                session.artifact_dir,
                _to_iso(session.created_at),
                _to_iso(session.updated_at),
            ),
        )
        self.connection.commit()
        return session

    def get_session(self, meeting_id: str) -> MeetingSession | None:
        row = self.connection.execute(
            "SELECT * FROM meeting_sessions WHERE id = ?", (meeting_id,)
        ).fetchone()
        return self._row_to_session(row) if row else None

    def list_sessions(self) -> list[MeetingSession]:
        rows = self.connection.execute(
            "SELECT * FROM meeting_sessions ORDER BY created_at, rowid"
        ).fetchall()
        return [self._row_to_session(r) for r in rows]

    def update_session(self, meeting_id: str, **fields: Any) -> MeetingSession:
        """Apply ``fields`` to an existing session and bump ``updated_at``.

        Raises :class:`KeyError` if the session does not exist.
        """
        session = self.get_session(meeting_id)
        if session is None:
            raise KeyError(f"Unknown meeting session: {meeting_id!r}")
        for key, value in fields.items():
            if not hasattr(session, key):
                raise AttributeError(f"MeetingSession has no field {key!r}")
            setattr(session, key, value)
        session.fallback_used = bool(session.fallback_used)
        session.updated_at = _utcnow()
        return self.create_session(session)

    @staticmethod
    def _row_to_session(row: sqlite3.Row) -> MeetingSession:
        return MeetingSession.from_dict(
            {
                "id": row["id"],
                "recall_bot_id": row["recall_bot_id"],
                "recall_recording_id": row["recall_recording_id"],
                "recall_transcript_id": row["recall_transcript_id"],
                "meeting_url_redacted": row["meeting_url_redacted"],
                "title": row["title"],
                "external_provider": row["external_provider"],
                "started_at": row["started_at"],
                "ended_at": row["ended_at"],
                "status": row["status"],
                "transcript_status": row["transcript_status"],
                "summary_status": row["summary_status"],
                "fallback_used": bool(row["fallback_used"]),
                "fallback_reason": row["fallback_reason"],
                "telegram_delivery_status": row["telegram_delivery_status"],
                "artifact_dir": row["artifact_dir"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )

    # ── participants ─────────────────────────────────────────────────────────
    def add_participant(self, participant: MeetingParticipant) -> MeetingParticipant:
        participant.id = participant.id or _new_id("part")
        self.connection.execute(
            """
            INSERT INTO meeting_participants (
                id, meeting_id, recall_participant_id, raw_display_name, email,
                canonical_name, telegram_username, telegram_user_id,
                match_confidence, match_reason
            ) VALUES (?,?,?,?,?,?,?,?,?,?)
            """,
            (
                participant.id,
                participant.meeting_id,
                participant.recall_participant_id,
                participant.raw_display_name,
                participant.email,
                participant.canonical_name,
                participant.telegram_username,
                participant.telegram_user_id,
                participant.match_confidence,
                participant.match_reason,
            ),
        )
        self.connection.commit()
        return participant

    def list_participants(self, meeting_id: str) -> list[MeetingParticipant]:
        rows = self.connection.execute(
            "SELECT * FROM meeting_participants WHERE meeting_id = ? ORDER BY rowid",
            (meeting_id,),
        ).fetchall()
        return [MeetingParticipant.from_dict(dict(r)) for r in rows]

    # ── transcript segments ──────────────────────────────────────────────────
    def add_transcript_segment(self, segment: TranscriptSegment) -> TranscriptSegment:
        segment.id = segment.id or _new_id("seg")
        self.connection.execute(
            """
            INSERT INTO transcript_segments (
                id, meeting_id, segment_index, start_ms, end_ms, speaker_raw,
                speaker_canonical, text, provider, confidence, raw_json
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                segment.id,
                segment.meeting_id,
                segment.segment_index,
                segment.start_ms,
                segment.end_ms,
                segment.speaker_raw,
                segment.speaker_canonical,
                segment.text,
                segment.provider,
                segment.confidence,
                _dump_json(segment.raw_json),
            ),
        )
        self.connection.commit()
        return segment

    def add_transcript_segments(
        self, segments: Iterable[TranscriptSegment]
    ) -> list[TranscriptSegment]:
        return [self.add_transcript_segment(s) for s in segments]

    def list_transcript_segments(self, meeting_id: str) -> list[TranscriptSegment]:
        rows = self.connection.execute(
            "SELECT * FROM transcript_segments WHERE meeting_id = ? "
            "ORDER BY segment_index, rowid",
            (meeting_id,),
        ).fetchall()
        out: list[TranscriptSegment] = []
        for r in rows:
            data = dict(r)
            data["raw_json"] = _load_json(data.get("raw_json"))
            out.append(TranscriptSegment.from_dict(data))
        return out

    # ── notes ────────────────────────────────────────────────────────────────
    def save_notes(self, notes: MeetingNotes) -> MeetingNotes:
        notes.id = notes.id or _new_id("notes")
        if notes.created_at is None:
            notes.created_at = _utcnow()
        self.connection.execute(
            """
            INSERT OR REPLACE INTO meeting_notes (
                id, meeting_id, executive_summary_json, executive_summary_markdown,
                action_items_json, decisions_json, open_questions_json, risks_json,
                model_provider, model_name, created_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                notes.id,
                notes.meeting_id,
                _dump_json(notes.executive_summary),
                notes.executive_summary_markdown,
                _dump_json(notes.action_items),
                _dump_json(notes.decisions),
                _dump_json(notes.open_questions),
                _dump_json(notes.risks),
                notes.model_provider,
                notes.model_name,
                _to_iso(notes.created_at),
            ),
        )
        self.connection.commit()
        return notes

    def get_notes(self, meeting_id: str) -> MeetingNotes | None:
        """Return the most recently saved notes for ``meeting_id``."""
        row = self.connection.execute(
            "SELECT * FROM meeting_notes WHERE meeting_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (meeting_id,),
        ).fetchone()
        if row is None:
            return None
        return MeetingNotes(
            id=row["id"],
            meeting_id=row["meeting_id"],
            executive_summary=_load_json(row["executive_summary_json"], default=[]),
            executive_summary_markdown=row["executive_summary_markdown"],
            action_items=_load_json(row["action_items_json"], default=[]),
            decisions=_load_json(row["decisions_json"], default=[]),
            open_questions=_load_json(row["open_questions_json"], default=[]),
            risks=_load_json(row["risks_json"], default=[]),
            model_provider=row["model_provider"],
            model_name=row["model_name"],
            created_at=row["created_at"],
        )

    # ── action items ─────────────────────────────────────────────────────────
    def add_action_item(self, item: MeetingActionItem) -> MeetingActionItem:
        item.id = item.id or _new_id("act")
        now = _utcnow()
        if item.created_at is None:
            item.created_at = now
        if item.updated_at is None:
            item.updated_at = now
        self.connection.execute(
            """
            INSERT OR REPLACE INTO meeting_action_items (
                id, meeting_id, owner_canonical, owner_telegram, task, due_date,
                priority, status, confidence, evidence_segment_ids,
                created_at, updated_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                item.id,
                item.meeting_id,
                item.owner_canonical,
                item.owner_telegram,
                item.task,
                item.due_date,
                item.priority,
                item.status,
                item.confidence,
                _dump_json(item.evidence_segment_ids),
                _to_iso(item.created_at),
                _to_iso(item.updated_at),
            ),
        )
        self.connection.commit()
        return item

    def list_action_items(self, meeting_id: str) -> list[MeetingActionItem]:
        rows = self.connection.execute(
            "SELECT * FROM meeting_action_items WHERE meeting_id = ? ORDER BY rowid",
            (meeting_id,),
        ).fetchall()
        out: list[MeetingActionItem] = []
        for r in rows:
            data = dict(r)
            data["evidence_segment_ids"] = _load_json(
                data.get("evidence_segment_ids"), default=[]
            )
            out.append(MeetingActionItem.from_dict(data))
        return out

    # ── telegram deliveries ──────────────────────────────────────────────────
    def add_delivery(self, delivery: TelegramDelivery) -> TelegramDelivery:
        delivery.id = delivery.id or _new_id("deliv")
        if delivery.delivered_at is None:
            delivery.delivered_at = _utcnow()
        self.connection.execute(
            """
            INSERT INTO telegram_deliveries (
                id, meeting_id, backend, chat_id, thread_id, message_ids,
                delivered_at, error
            ) VALUES (?,?,?,?,?,?,?,?)
            """,
            (
                delivery.id,
                delivery.meeting_id,
                delivery.backend,
                delivery.chat_id,
                delivery.thread_id,
                _dump_json(delivery.message_ids),
                _to_iso(delivery.delivered_at),
                delivery.error,
            ),
        )
        self.connection.commit()
        return delivery

    def list_deliveries(self, meeting_id: str) -> list[TelegramDelivery]:
        rows = self.connection.execute(
            "SELECT * FROM telegram_deliveries WHERE meeting_id = ? ORDER BY rowid",
            (meeting_id,),
        ).fetchall()
        return [
            TelegramDelivery(
                id=r["id"],
                meeting_id=r["meeting_id"],
                backend=r["backend"],
                chat_id=r["chat_id"],
                thread_id=r["thread_id"],
                message_ids=_load_json(r["message_ids"], default=[]),
                delivered_at=r["delivered_at"],
                error=r["error"],
            )
            for r in rows
        ]

    # ── artifact paths ───────────────────────────────────────────────────────
    def artifact_path(
        self, meeting_id: str, *relative_parts: str, create_parents: bool = False
    ) -> Path:
        """Resolve a path for a meeting artifact, kept inside ``storage_dir``.

        Returns ``storage_dir / <meeting_id> / <relative_parts...>``.  Rejects
        absolute components and ``..`` traversal that would escape the storage
        root (raising :class:`ArtifactPathError`).  With ``create_parents`` the
        parent directory is created.
        """
        if not str(meeting_id).strip():
            raise ValueError("meeting_id is required for an artifact path.")

        parts = [str(meeting_id), *(str(p) for p in relative_parts)]
        for part in parts:
            if Path(part).is_absolute():
                raise ArtifactPathError(
                    f"Artifact path component must be relative, got {part!r}."
                )
            if ".." in Path(part).parts:
                raise ArtifactPathError(
                    f"Artifact path may not contain '..', got {part!r}."
                )

        # Validate containment against the *resolved* root so that ``..`` and
        # symlinks cannot escape; return a path anchored to the configured
        # storage_dir so callers see a stable, predictable location.
        resolved = self._root.joinpath(*parts).resolve()
        if resolved != self._root and not resolved.is_relative_to(self._root):
            raise ArtifactPathError(
                f"Artifact path escapes the storage directory: {'/'.join(parts)!r}."
            )

        path = self.storage_dir.joinpath(*parts)
        if create_parents:
            path.parent.mkdir(parents=True, exist_ok=True)
        return path


__all__ = [
    "ArtifactPathError",
    "DB_FILENAME",
    "MeetingNotes",
    "MeetingStore",
    "TelegramDelivery",
]
