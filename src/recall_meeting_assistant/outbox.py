"""Durable, secret-safe outbox messages for post-meeting delivery."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from recall_meeting_assistant.storage import MeetingStore

DEFAULT_LEFT_MEETING_TEXT = "Meeting ended. Processing transcript..."
OUTBOX_DIR = "outbox"
LEFT_MEETING_OUTBOX_FILENAME = "meeting_left_notification.json"
TRANSCRIPT_OUTBOX_FILENAME = "meeting_transcript_notification.json"
SUMMARY_OUTBOX_FILENAME = "meeting_summary_notification.json"
SUMMARY_EMAIL_OUTBOX_FILENAME = "meeting_summary_email_notification.json"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class OutboxMessage:
    """A queued message consumed by a configured delivery adapter."""

    meeting_id: str
    kind: Literal["meeting_left", "meeting_transcript", "meeting_summary"]
    text: str
    backend: str = "telegram"
    chat_id: str | None = None
    thread_id: str | None = None
    recipients: list[str] = field(default_factory=list)
    subject: str | None = None
    html_body: str | None = None
    attachments: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utcnow_iso)
    status: Literal["queued"] = "queued"

    def __post_init__(self) -> None:
        if not str(self.meeting_id).strip():
            raise ValueError("OutboxMessage.meeting_id is required.")
        if not str(self.text).strip():
            raise ValueError("OutboxMessage.text is required.")
        if any(not str(recipient).strip() for recipient in self.recipients):
            raise ValueError("OutboxMessage recipients must not contain blank values.")
        for attachment in self.attachments:
            path = Path(str(attachment))
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("OutboxMessage attachments must be relative artifact paths.")

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "meeting_id": self.meeting_id,
            "kind": self.kind,
            "text": self.text,
            "backend": self.backend,
            "chat_id": self.chat_id,
            "thread_id": self.thread_id,
            "attachments": list(self.attachments),
            "created_at": self.created_at,
            "status": self.status,
        }
        if self.recipients:
            payload["recipients"] = list(self.recipients)
        if self.subject is not None:
            payload["subject"] = self.subject
        if self.html_body is not None:
            payload["html_body"] = self.html_body
        return payload


def write_outbox_message(
    store: MeetingStore,
    message: OutboxMessage,
    *,
    filename: str,
) -> str:
    """Persist an idempotent outbox payload under the meeting artifact folder."""

    path = store.artifact_path(message.meeting_id, OUTBOX_DIR, filename, create_parents=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(message.to_dict(), ensure_ascii=False, indent=2) + "\n")
    tmp.replace(path)
    return str(path)


def queue_left_meeting_notification(
    store: MeetingStore,
    *,
    meeting_id: str,
    text: str = DEFAULT_LEFT_MEETING_TEXT,
    backend: str = "telegram",
    chat_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Queue a short status message after the bot leaves the meeting."""

    return write_outbox_message(
        store,
        OutboxMessage(
            meeting_id=meeting_id,
            kind="meeting_left",
            text=text,
            backend=backend,
            chat_id=chat_id,
            thread_id=thread_id,
        ),
        filename=LEFT_MEETING_OUTBOX_FILENAME,
    )


def queue_transcript_notification(
    store: MeetingStore,
    *,
    meeting_id: str,
    text: str,
    backend: str = "telegram",
    chat_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Queue the complete normalized transcript for delivery."""

    return write_outbox_message(
        store,
        OutboxMessage(
            meeting_id=meeting_id,
            kind="meeting_transcript",
            text=text,
            backend=backend,
            chat_id=chat_id,
            thread_id=thread_id,
        ),
        filename=TRANSCRIPT_OUTBOX_FILENAME,
    )


def queue_summary_notification(
    store: MeetingStore,
    *,
    meeting_id: str,
    text: str,
    backend: str = "telegram",
    chat_id: str | None = None,
    thread_id: str | None = None,
) -> str:
    """Queue an optional summary supplied by Recall or another adapter."""

    return write_outbox_message(
        store,
        OutboxMessage(
            meeting_id=meeting_id,
            kind="meeting_summary",
            text=text,
            backend=backend,
            chat_id=chat_id,
            thread_id=thread_id,
        ),
        filename=SUMMARY_OUTBOX_FILENAME,
    )


def queue_summary_email_notification(
    store: MeetingStore,
    *,
    meeting_id: str,
    text: str,
    recipients: list[str],
    subject: str,
    html_body: str | None = None,
    backend: str = "gmail",
) -> str:
    """Queue a summary email for an already filtered recipient list."""

    if not recipients:
        raise ValueError("At least one summary email recipient is required.")
    return write_outbox_message(
        store,
        OutboxMessage(
            meeting_id=meeting_id,
            kind="meeting_summary",
            text=text,
            backend=backend,
            recipients=list(recipients),
            subject=subject,
            html_body=html_body,
        ),
        filename=SUMMARY_EMAIL_OUTBOX_FILENAME,
    )


__all__ = [
    "DEFAULT_LEFT_MEETING_TEXT",
    "LEFT_MEETING_OUTBOX_FILENAME",
    "TRANSCRIPT_OUTBOX_FILENAME",
    "SUMMARY_OUTBOX_FILENAME",
    "SUMMARY_EMAIL_OUTBOX_FILENAME",
    "OUTBOX_DIR",
    "OutboxMessage",
    "queue_left_meeting_notification",
    "queue_transcript_notification",
    "queue_summary_notification",
    "queue_summary_email_notification",
    "write_outbox_message",
]
