"""Tests for Recall.ai Phase 1 post-meeting ingest automation.

Covers the automation gap after webhook receipt: transcript.done should fetch,
normalize, persist, and quality-gate transcripts without doing real network or
OpenAI calls in tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from recall_meeting_assistant.ingest import (
    OUTBOX_LEFT_MEETING_KEY,
    OUTBOX_TRANSCRIPT_KEY,
    IngestStatus,
    process_recall_webhook,
    redact_status_value,
)
from recall_meeting_assistant.openai_fallback import FallbackResult
from recall_meeting_assistant.outbox import DEFAULT_LEFT_MEETING_TEXT
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.transcript import REASON_NO_SEGMENTS
from recall_meeting_assistant.webhooks import WebhookEvent, parse_event


class FakeRecallClient:
    def __init__(self, *, transcript_resource: dict[str, Any] | None = None, bot: dict[str, Any] | None = None):
        self.transcript_resource = transcript_resource or {}
        self.bot = bot or {}
        self.fetched_transcript_ids: list[str] = []
        self.fetched_bot_ids: list[str] = []

    def get_recording_resource(self, resource_id: str, *, resource_type: str = "transcript_artifact", include_transcript: bool = True):
        self.fetched_transcript_ids.append(resource_id)
        return self.transcript_resource

    def get_bot(self, bot_id: str):
        self.fetched_bot_ids.append(bot_id)
        return self.bot


class FakeFallback:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def __call__(self, **kwargs: Any) -> FallbackResult:
        self.calls.append(kwargs)
        meeting_id = kwargs["meeting_id"]
        from recall_meeting_assistant.models import TranscriptSegment

        return FallbackResult(
            success=True,
            trigger_reason=kwargs.get("trigger_reason"),
            segments=[
                TranscriptSegment(
                    meeting_id=meeting_id,
                    segment_index=0,
                    start_ms=0,
                    end_ms=1000,
                    speaker_raw=None,
                    provider="openai",
                    text="Fallback transcript from recording audio.",
                )
            ],
        )


def _event(transcript_payload: dict[str, Any] | None = None) -> WebhookEvent:
    payload = {
        "event": "transcript.done",
        "data": {
            "bot": {"id": "bot_123"},
            "recording": {"id": "rec_123"},
            "transcript": {"id": "tr_123"},
        },
    }
    if transcript_payload:
        payload["data"]["transcript"].update(transcript_payload)
    return parse_event(payload)


def _resource_with_transcript(transcript: Any) -> dict[str, Any]:
    return {
        "id": "tr_123",
        "recording": {"id": "rec_123"},
        "transcript": transcript,
        "download_url": "https://storage.example.com/path/video.mp4?X-Amz-Signature=example-signature",
    }


def test_transcript_done_ingest_stores_raw_normalized_markdown_and_segments(tmp_path: Path):
    store = MeetingStore(tmp_path)
    event = _event()
    client = FakeRecallClient(
        transcript_resource=_resource_with_transcript(
            [
                {"speaker": "Speaker A", "start": 0, "end": 2, "text": "We should ship the ingest automation today."},
                {"speaker": "Speaker B", "start": 2, "end": 5, "text": "Agree, the Recall transcript pipeline is ready."},
            ]
        ),
        bot={"id": "bot_123", "meeting_url": "https://meet.google.com/example-meeting"},
    )

    result = process_recall_webhook(event, client=client, store=store)

    assert result.status == IngestStatus.COMPLETED
    assert result.meeting_id is not None
    assert result.provider == "recallai_streaming"
    assert result.fallback_used is False
    assert client.fetched_transcript_ids == ["tr_123"]
    assert client.fetched_bot_ids == ["bot_123"]

    session = store.get_session(result.meeting_id)
    assert session is not None
    assert session.recall_bot_id == "bot_123"
    assert session.recall_recording_id == "rec_123"
    assert session.recall_transcript_id == "tr_123"
    assert session.transcript_status == "done"
    assert session.meeting_url_redacted == "https://meet.google.com/***"

    segments = store.list_transcript_segments(result.meeting_id)
    assert [s.speaker_raw for s in segments] == ["Speaker A", "Speaker B"]
    assert "ship the ingest automation" in segments[0].text

    assert set(result.artifact_paths) == {
        "raw_bot_json",
        "raw_transcript_resource_json",
        "raw_transcript_json",
        "normalized_transcript_json",
        "transcript_markdown",
        "outbox_transcript_json",
    }
    normalized = json.loads(Path(result.artifact_paths["normalized_transcript_json"]).read_text())
    assert normalized["segment_count"] == 2
    markdown = Path(result.artifact_paths["transcript_markdown"]).read_text()
    assert "Speaker A" in markdown
    assert "Speaker B" in markdown
    telegram = Path(result.artifact_paths[OUTBOX_TRANSCRIPT_KEY]).read_text()
    assert "ship the ingest automation" in telegram


def test_transcript_done_downloads_signed_recall_transcript_payload(tmp_path: Path, monkeypatch):
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return None

        def read(self):
            return json.dumps(
                [
                    {
                        "participant": {"name": "Speaker A"},
                        "words": [
                            {
                                "text": "Downloaded",
                                "start_timestamp": {"relative": 0},
                                "end_timestamp": {"relative": 250},
                            },
                            {
                                "text": "transcript",
                                "start_timestamp": {"relative": 250},
                                "end_timestamp": {"relative": 750},
                            },
                        ],
                    }
                ]
            ).encode()

    seen: dict[str, Any] = {}

    def fake_urlopen(url: str, timeout: int):
        seen["url"] = url
        seen["timeout"] = timeout
        return _Response()

    monkeypatch.setattr("recall_meeting_assistant.ingest.urllib.request.urlopen", fake_urlopen)
    store = MeetingStore(tmp_path)
    event = _event()
    client = FakeRecallClient(
        transcript_resource={
            "id": "tr_123",
            "recording": {"id": "rec_123"},
            "data": {"download_url": "https://signed.example.com/transcript.json?X-Amz-Signature=example-signature"},
        }
    )

    result = process_recall_webhook(event, client=client, store=store)

    assert result.status == IngestStatus.COMPLETED
    assert result.meeting_id is not None
    assert seen == {"url": "https://signed.example.com/transcript.json?X-Amz-Signature=example-signature", "timeout": 30}
    segments = store.list_transcript_segments(result.meeting_id)
    assert len(segments) == 1
    assert segments[0].speaker_raw == "Speaker A"
    assert segments[0].text == "Downloaded transcript"


def test_empty_transcript_triggers_injected_fallback_and_stores_fallback_segments(tmp_path: Path):
    store = MeetingStore(tmp_path)
    fallback = FakeFallback()
    client = FakeRecallClient(transcript_resource=_resource_with_transcript([]))

    result = process_recall_webhook(
        _event(),
        client=client,
        store=store,
        fallback_transcriber=fallback,
        fallback_audio=b"fake audio bytes",
    )

    assert result.status == IngestStatus.COMPLETED
    assert result.fallback_used is True
    assert result.provider == "openai"
    assert REASON_NO_SEGMENTS in result.fallback_reason
    assert len(fallback.calls) == 1
    assert fallback.calls[0]["trigger_reason"] == REASON_NO_SEGMENTS

    session = store.get_session(result.meeting_id)
    assert session is not None
    assert session.fallback_used is True
    assert session.fallback_reason == REASON_NO_SEGMENTS
    segments = store.list_transcript_segments(result.meeting_id)
    assert len(segments) == 1
    assert segments[0].provider == "openai"
    assert "Fallback transcript" in segments[0].text


def test_package_exports_ingest_symbols():
    import recall_meeting_assistant as pkg

    assert pkg.process_recall_webhook is process_recall_webhook
    assert pkg.IngestStatus.COMPLETED == IngestStatus.COMPLETED
    assert callable(pkg.ingest_completed_transcript)


def test_unsupported_webhook_event_noops_without_fetch_or_artifacts(tmp_path: Path):
    store = MeetingStore(tmp_path)
    client = FakeRecallClient()
    event = parse_event({"event": "video_mixed.processing", "data": {"bot": {"id": "bot_123"}}})

    result = process_recall_webhook(event, client=client, store=store)

    assert result.status == IngestStatus.NOOP
    assert result.meeting_id is None
    assert result.artifact_paths == {}
    assert client.fetched_transcript_ids == []
    assert client.fetched_bot_ids == []
    assert store.list_sessions() == []


def test_terminal_bot_status_queues_left_meeting_notification_outbox(tmp_path: Path):
    store = MeetingStore(tmp_path)
    client = FakeRecallClient(bot={"id": "bot_123", "meeting_url": "https://meet.google.com/example-meeting"})
    event = parse_event(
        {
            "event": "bot.call_ended",
            "data": {
                "bot": {"id": "bot_123"},
                "status": {"code": "call_ended"},
            },
        }
    )

    result = process_recall_webhook(
        event,
        client=client,
        store=store,
        delivery_chat_id="-1001234567890",
        delivery_thread_id="42",
    )

    assert result.status == IngestStatus.COMPLETED
    assert result.reason == "meeting_left_notification_queued"
    assert result.meeting_id == "recall_bot_123"
    assert client.fetched_bot_ids == ["bot_123"]
    session = store.get_session("recall_bot_123")
    assert session is not None
    assert session.status == "left"
    assert session.summary_status == "pending"
    assert session.telegram_delivery_status == "queued"
    assert session.meeting_url_redacted == "https://meet.google.com/***"

    outbox_path = Path(result.artifact_paths[OUTBOX_LEFT_MEETING_KEY])
    payload = json.loads(outbox_path.read_text())
    assert payload["kind"] == "meeting_left"
    assert payload["text"] == DEFAULT_LEFT_MEETING_TEXT
    assert payload["chat_id"] == "-1001234567890"
    assert payload["thread_id"] == "42"
    assert "example-meeting" not in outbox_path.read_text()


def test_non_left_bot_status_noops_without_notification(tmp_path: Path):
    store = MeetingStore(tmp_path)
    client = FakeRecallClient()
    event = parse_event(
        {
            "event": "bot.in_call_recording",
            "data": {
                "bot": {"id": "bot_123"},
                "status": {"code": "in_call_recording"},
            },
        }
    )

    result = process_recall_webhook(event, client=client, store=store)

    assert result.status == IngestStatus.NOOP
    assert result.reason == "unsupported_bot_status"
    assert client.fetched_bot_ids == []
    assert store.list_sessions() == []


def test_status_redaction_removes_meeting_urls_and_signed_urls(tmp_path: Path):
    text = "https://meet.google.com/example-meeting and https://storage.example.com/a.mp4?X-Amz-Signature=example-signature&token=abc"

    redacted = redact_status_value(text)

    assert "example-meeting" not in redacted
    assert "X-Amz-Signature" not in redacted
    assert "token=abc" not in redacted
    assert "https://meet.google.com/***" in redacted
    assert "https://storage.example.com/***" in redacted
