"""Tests for exact summary recipients and Gmail outbox delivery."""

from __future__ import annotations

import base64
import json
from email import message_from_bytes
from email.policy import default
from pathlib import Path

import pytest

from recall_meeting_assistant import email_delivery
from recall_meeting_assistant.email_delivery import (
    GMAIL_SCOPES,
    iter_pending,
    load_gmail_credentials,
    make_gmail_sender,
    run,
)
from recall_meeting_assistant.outbox import SUMMARY_EMAIL_OUTBOX_FILENAME
from recall_meeting_assistant.recall_summary import ingest_recall_summary
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.summary_recipients import (
    SUMMARY_EMAIL_ALLOWLIST_ENV,
    allowlisted_summary_recipients,
    get_summary_email_allowlist,
    summary_recipient_emails_from_bot,
    summary_recipients_from_event,
)
from recall_meeting_assistant.telegram import iter_pending as telegram_iter_pending

ALLOWLIST = [
    "person.one@example.com",
    "person.two@example.com",
    "person.three@example.com",
    "person.four@example.com",
    "person.five@example.com",
    "person.six@example.com",
    "person.seven@example.com",
    "person.eight@example.com",
    "person.nine@example.com",
]


@pytest.fixture(autouse=True)
def configured_summary_email_allowlist(monkeypatch):
    monkeypatch.setenv(SUMMARY_EMAIL_ALLOWLIST_ENV, ",".join(ALLOWLIST))


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


def test_load_gmail_credentials_rejects_broader_stored_scopes(tmp_path: Path):
    token_path = tmp_path / "google_token.json"
    token_path.write_text(
        json.dumps(
            {
                "token": "access-token",
                "refresh_token": "refresh-token",
                "client_id": "client-id",
                "client_secret": "client-secret",
                "scopes": [
                    "https://www.googleapis.com/auth/gmail.send",
                    "https://www.googleapis.com/auth/gmail.modify",
                ],
                "expiry": "2099-01-01T00:00:00Z",
            }
        )
    )

    with pytest.raises(RuntimeError, match="broader_scopes_require_reauth"):
        load_gmail_credentials(token_path)


def test_google_token_path_defaults_to_product_home(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("GOOGLE_TOKEN_PATH", raising=False)
    monkeypatch.setattr(email_delivery, "get_product_home", lambda: tmp_path)
    assert email_delivery._default_token_path() == tmp_path / "google_token.json"


def test_google_token_path_environment_override(monkeypatch, tmp_path: Path):
    configured = tmp_path / "custom-google-token.json"
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(configured))
    assert email_delivery._default_token_path() == configured


def test_event_selection_intersects_exact_allowlist_and_deduplicates():
    event = {
        "attendees": [
            {"email": "Person.One@example.com"},
            {"email": "outsider@example.com"},
            {"email": "person.two@example.com"},
            {"email": "person.two@example.com"},
        ],
        "organizer": {"email": "organizer@example.com"},
    }
    assert summary_recipients_from_event(event) == [
        "person.one@example.com",
        "person.two@example.com",
    ]
    assert allowlisted_summary_recipients(ALLOWLIST + ["not-allowed@example.com"]) == ALLOWLIST
    assert get_summary_email_allowlist() == frozenset(ALLOWLIST)


def test_empty_summary_email_allowlist_disables_delivery(monkeypatch, tmp_path: Path):
    monkeypatch.setenv(SUMMARY_EMAIL_ALLOWLIST_ENV, "")
    assert get_summary_email_allowlist() == frozenset()
    assert allowlisted_summary_recipients(ALLOWLIST) == []
    assert summary_recipients_from_event(
        {"attendees": [{"email": "person.one@example.com"}]}
    ) == []

    store = MeetingStore(tmp_path)
    result = ingest_recall_summary(
        store,
        meeting_id="meeting-disabled",
        resource={"summary": {"title": "Sync", "short_summary": "Recap."}},
        bot_payload={
            "metadata": {"summary_recipient_emails": ["person.one@example.com"]}
        },
    )
    assert "outbox_summary_email_json" not in result.artifact_paths


def test_allowlist_is_read_from_environment_for_each_selection(monkeypatch):
    monkeypatch.setenv(
        SUMMARY_EMAIL_ALLOWLIST_ENV,
        " Person.Two@example.com, person.two@example.com, person.three@example.com ",
    )
    assert get_summary_email_allowlist() == frozenset(
        {"person.two@example.com", "person.three@example.com"}
    )
    assert allowlisted_summary_recipients(ALLOWLIST) == [
        "person.two@example.com",
        "person.three@example.com",
    ]


def test_actual_recall_participants_override_invite_fallback():
    metadata = {
        "summary_recipient_emails": ["person.two@example.com", "person.one@example.com"]
    }
    assert summary_recipient_emails_from_bot(
        {"meeting_participants": [{"email": "outsider@example.com"}], "metadata": metadata}
    ) == []
    assert summary_recipient_emails_from_bot({"metadata": metadata}) == [
        "person.two@example.com",
        "person.one@example.com",
    ]


def test_metadata_json_string_is_parsed_for_summary_recipients():
    metadata = {
        "summary_recipient_emails": (
            '["person.two@example.com", "person.one@example.com", '
            '"outsider@example.com"]'
        )
    }

    assert summary_recipient_emails_from_bot({"metadata": metadata}) == [
        "person.two@example.com",
        "person.one@example.com",
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
                    "person.one@example.com",
                    "outsider@example.com",
                    "person.two@example.com",
                ]
            }
        },
    )
    assert result.available is True
    email_path = Path(result.artifact_paths["outbox_summary_email_json"])
    assert email_path.name == SUMMARY_EMAIL_OUTBOX_FILENAME
    payload = json.loads(email_path.read_text())
    assert payload["backend"] == "gmail"
    assert payload["recipients"] == ["person.one@example.com", "person.two@example.com"]
    assert "outsider@example.com" not in email_path.read_text()


def test_gmail_sender_builds_multipart_message_and_rejects_outsider():
    service = FakeGmail()
    sender = make_gmail_sender(service)
    message_id = sender(
        text="**Summary**\n• item",
        payload={
            "recipients": ["person.one@example.com"],
            "subject": "Meeting summary: Sync",
        },
    )
    assert message_id == "gmail-message-1"
    raw = service.users_resource.messages_resource.body["raw"]
    parsed = message_from_bytes(base64.urlsafe_b64decode(raw), policy=default)
    assert parsed["To"] == "person.one@example.com"
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
                "recipients": ["person.two@example.com"],
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
        summary_recipient_emails=["person.two@example.com"],
    )
    assert "outbox_summary_email_json" not in result.artifact_paths
    assert not (tmp_path / "meeting-2" / "outbox" / SUMMARY_EMAIL_OUTBOX_FILENAME).exists()
