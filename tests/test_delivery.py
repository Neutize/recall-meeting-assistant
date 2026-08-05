"""Tests for the outbox delivery adapter."""

from __future__ import annotations

import json
from pathlib import Path

from recall_meeting_assistant.delivery import (
    DELIVERED_STATUS,
    FAILED_STATUS,
    deliver_outbox,
    deliver_outbox_file,
    fake_sender,
)
from recall_meeting_assistant.models import MeetingSession
from recall_meeting_assistant.outbox import (
    queue_left_meeting_notification,
    queue_summary_notification,
)
from recall_meeting_assistant.storage import MeetingStore


def test_deliver_outbox_file_marks_delivered_and_records_delivery(tmp_path: Path):
    store = MeetingStore(tmp_path)
    store.create_session(MeetingSession(id="recall_bot_1"))
    outbox_path = queue_left_meeting_notification(
        store,
        meeting_id="recall_bot_1",
        chat_id="-1001234567890",
        thread_id="42",
    )

    calls = []

    def sender(**kwargs):
        calls.append(kwargs)
        return "tg-42"

    outcome = deliver_outbox_file(outbox_path, sender=sender, store=store)

    assert outcome.status == DELIVERED_STATUS
    assert outcome.message_ids == ["tg-42"]
    assert calls[0]["text"] == "Meeting ended. Processing transcript..."
    assert calls[0]["chat_id"] == "-1001234567890"
    assert calls[0]["thread_id"] == "42"

    payload = json.loads(Path(outbox_path).read_text())
    assert payload["status"] == DELIVERED_STATUS
    assert payload["message_ids"] == ["tg-42"]
    assert payload["attempts"] == 1
    assert payload["delivered_at"].endswith("Z")

    deliveries = store.list_deliveries("recall_bot_1")
    assert deliveries[0].message_ids == ["tg-42"]
    assert store.get_session("recall_bot_1").telegram_delivery_status == DELIVERED_STATUS


def test_deliver_outbox_file_marks_failed_and_keeps_retryable_payload(tmp_path: Path):
    store = MeetingStore(tmp_path)
    outbox_path = queue_summary_notification(store, meeting_id="recall_bot_2", text="summary")

    def sender(**kwargs):
        raise RuntimeError("telegram temporarily unavailable")

    outcome = deliver_outbox_file(outbox_path, sender=sender, store=store)

    assert outcome.status == FAILED_STATUS
    assert outcome.error == "RuntimeError: delivery_failed"
    payload = json.loads(Path(outbox_path).read_text())
    assert payload["status"] == FAILED_STATUS
    assert payload["attempts"] == 1
    assert payload["error"] == "RuntimeError: delivery_failed"


def test_deliver_outbox_skips_already_delivered(tmp_path: Path):
    store = MeetingStore(tmp_path)
    outbox_path = Path(queue_left_meeting_notification(store, meeting_id="recall_bot_3"))
    payload = json.loads(outbox_path.read_text())
    payload["status"] = DELIVERED_STATUS
    payload["message_ids"] = ["old"]
    outbox_path.write_text(json.dumps(payload))

    calls = []
    outcomes = deliver_outbox(tmp_path, sender=lambda **kwargs: calls.append(kwargs) or "new")

    assert outcomes[0].status == DELIVERED_STATUS
    assert outcomes[0].message_ids == ["old"]
    assert calls == []


def test_fake_sender_smoke_consumes_existing_outbox_file(tmp_path: Path):
    store = MeetingStore(tmp_path)
    outbox_path = queue_left_meeting_notification(store, meeting_id="recall_bot_smoke")

    outcome = deliver_outbox_file(outbox_path, sender=fake_sender)

    assert outcome.status == DELIVERED_STATUS
    assert outcome.message_ids == ["fake-message-id"]
    assert json.loads(Path(outbox_path).read_text())["status"] == DELIVERED_STATUS
