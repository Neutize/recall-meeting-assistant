"""Tests for exact summary recipients and Gmail outbox delivery."""

from __future__ import annotations

import base64
import json
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest

from recall_meeting_assistant.email_delivery import (
    GMAIL_SCOPES,
    iter_pending,
    make_gmail_sender,
    run,
)
from recall_meeting_assistant.outbox import SUMMARY_EMAIL_OUTBOX_FILENAME
from recall_meeting_assistant.recall_summary import ingest_recall_summary
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.summary_recipients import (
    SUMMARY_EMAIL_ALLOWLIST,
    allowlisted_summary_recipients,
    summary_recipient_emails_from_bot,
    summary_recipients_from_event,
)
from recall_meeting_assistant.telegram import iter_pending as telegram_iter_pending

ALLOWLIST = [
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
    "redacted@example.invalid",
]


class FakeMessages:
    def __init__(self):
        self.body = None

    def send(self, *, userId, body):
        self.body = body
        return self

    def execute(self):
        return {"id": "gmail-message-1"}


class FakeUsers:
    def __init__(self):
        self.messages_resource = FakeMessages()

    def messages(self):
        return self.messages_resource


class FakeGmail:
    def __init__(self):
        self.users_resource = FakeUsers()

    def users(self):
        return self.users_resource


def test_gmail_uses_send_only_scope():
    assert GMAIL_SCOPES == ["https://www.googleapis.com/auth/gmail.send"]


def test_event_selection_intersects_exact_allowlist_and_deduplicates():
    event = {
        "attendees": [
            {"email": "redacted@example.invalid"},
            {"email": "outsider@example.com"},
            {"email": "redacted@example.invalid"},
            {"email": "redacted@example.invalid"},
        ],
        "organizer": {"email": "organizer@example.com"},
    }
    assert summary_recipients_from_event(event) == ["redacted@example.invalid", "redacted@example.invalid"]
    assert allowlisted_summary_recipients(ALLOWLIST + ["not-allowed@example.com"]) == ALLOWLIST
    assert len(SUMMARY_EMAIL_ALLOWLIST) == 9


def test_actual_recall_participants_override_invite_fallback():
    metadata = {"summary_recipient_emails": ["redacted@example.invalid", "redacted@example.invalid"]}
    assert summary_recipient_emails_from_bot(
        {"meeting_participants": [{"email": "outsider@example.com"}], "metadata": metadata}
    ) == []
    assert summary_recipient_emails_from_bot({"metadata": metadata}) == [
        "redacted@example.invalid",
        "redacted@example.invalid",
    ]


def test_metadata_json_string_is_parsed_for_summary_recipients():
    metadata = {
        "summary_recipient_emails": '["redacted@example.invalid", "redacted@example.invalid", "outsider@example.com"]'
    }

    assert summary_recipient_emails_from_bot({"metadata": metadata}) == [
        "redacted@example.invalid",
        "redacted@example.invalid",
    ]


def test_summary_ingest_queues_filtered_email_outbox(tmp_path: Path):
    store = MeetingStore(tmp_path)
    result = ingest_recall_summary(
        store,
        meeting_id="meeting-1",
        resource={"summary": {"title": "Sync", "short_summary": "A short recap."}},
        bot_payload={
            "metadata": {
                "summary_recipient_emails": [
                    "redacted@example.invalid",
                    "outsider@example.com",
                    "redacted@example.invalid",
                ]
            }
        },
    )
    assert result.available is True
    email_path = Path(result.artifact_paths["outbox_summary_email_json"])
    assert email_path.name == SUMMARY_EMAIL_OUTBOX_FILENAME
    payload = json.loads(email_path.read_text())
    assert payload["backend"] == "gmail"
    assert payload["recipients"] == ["redacted@example.invalid", "redacted@example.invalid"]
    assert "outsider@example.com" not in email_path.read_text()


def test_gmail_sender_builds_multipart_message_and_rejects_outsider():
    service = FakeGmail()
    sender = make_gmail_sender(service)
    message_id = sender(
        text="**Summary**\n• item",
        payload={
            "recipients": ["redacted@example.invalid"],
            "subject": "Meeting summary: Sync",
        },
    )
    assert message_id == "gmail-message-1"
    raw = service.users_resource.messages_resource.body["raw"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)
    assert parsed["To"] == "redacted@example.invalid"
    assert parsed["Subject"] == "Meeting summary: Sync"
    assert "Summary" in parsed.get_body("plain").get_content()
    assert "<strong>Summary</strong>" in parsed.get_body("html").get_content()

    with pytest.raises(RuntimeError, match="not_allowlisted"):
        sender(
            text="summary",
            payload={"recipients": ["outsider@example.com"], "subject": "bad"},
        )


def test_email_watcher_delivers_only_gmail_outboxes(tmp_path: Path):
    email_dir = tmp_path / "meeting-1" / "outbox"
    email_dir.mkdir(parents=True)
    (email_dir / "summary-email.json").write_text(
        json.dumps(
            {
                "meeting_id": "meeting-1",
                "kind": "meeting_summary",
                "text": "summary",
                "backend": "gmail",
                "recipients": ["redacted@example.invalid"],
                "subject": "Meeting summary",
                "status": "queued",
            }
        )
    )
    (email_dir / "telegram-summary.json").write_text(
        json.dumps(
            {
                "meeting_id": "meeting-1",
                "kind": "meeting_summary",
                "text": "summary",
                "backend": "telegram",
                "status": "queued",
            }
        )
    )
    assert len(iter_pending(tmp_path)) == 1
    assert [payload.backend for payload in telegram_iter_pending(tmp_path)] == ["telegram"]
    service = FakeGmail()
    assert run(storage_root=tmp_path, service=service) == 0
    payload = json.loads((email_dir / "summary-email.json").read_text())
    assert payload["status"] == "delivered"
    assert iter_pending(tmp_path) == []


def test_empty_recipient_selection_does_not_queue_email(tmp_path: Path):
    store = MeetingStore(tmp_path)
    result = ingest_recall_summary(
        store,
        meeting_id="meeting-2",
        resource={"summary": {"title": "No allowlisted attendees", "short_summary": "Recap."}},
        bot_payload={"meeting_participants": [{"email": "outsider@example.com"}]},
        summary_recipient_emails=["redacted@example.invalid"],
    )
    assert "outbox_summary_email_json" not in result.artifact_paths
    assert not (tmp_path / "meeting-2" / "outbox" / SUMMARY_EMAIL_OUTBOX_FILENAME).exists()
