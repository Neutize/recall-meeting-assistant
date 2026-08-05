"""Tests for safe Telegram-delivery outbox artifacts."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recall_meeting_assistant.outbox import (
    DEFAULT_LEFT_MEETING_TEXT,
    LEFT_MEETING_OUTBOX_FILENAME,
    OUTBOX_DIR,
    OutboxMessage,
    queue_left_meeting_notification,
)
from recall_meeting_assistant.storage import MeetingStore


def test_queue_left_meeting_notification_writes_safe_outbox_payload(tmp_path: Path):
    store = MeetingStore(tmp_path)

    path = queue_left_meeting_notification(
        store,
        meeting_id="recall_bot_123",
        chat_id="-1001234567890",
        thread_id="42",
    )

    assert path.endswith(f"recall_bot_123/{OUTBOX_DIR}/{LEFT_MEETING_OUTBOX_FILENAME}")
    payload = json.loads(Path(path).read_text())
    assert payload == {
        "meeting_id": "recall_bot_123",
        "kind": "meeting_left",
        "text": DEFAULT_LEFT_MEETING_TEXT,
        "backend": "telegram",
        "chat_id": "-1001234567890",
        "thread_id": "42",
        "attachments": [],
        "created_at": payload["created_at"],
        "status": "queued",
    }
    assert payload["created_at"].endswith("Z")


def test_outbox_message_rejects_unsafe_attachment_paths():
    with pytest.raises(ValueError, match="attachments must be relative"):
        OutboxMessage(
            meeting_id="recall_bot_123",
            kind="meeting_summary",
            text="summary",
            attachments=["../secret.env"],
        )
