"""Tests for the Recall.ai meeting-assistant API client.

Covers Task 2 of docs/architecture.md:

* ``create_bot()`` builds the correct regional URL and request payload.
* The ``Authorization`` header carries the ``Token`` scheme required by the live regional REST API.
* A 201 response yields a bot record.
* 400 / 401 / 507 responses raise clear, typed errors.
* All HTTP is mocked (``httpx.MockTransport``) — never a real Recall call,
  and the API key never leaks into errors or stdout.
"""

from __future__ import annotations

import json

import httpx
import pytest

from recall_meeting_assistant.client import (
    DEFAULT_TRANSCRIPT_MODE,
    RecallAPIError,
    RecallAuthError,
    RecallBadRequestError,
    RecallBot,
    RecallClient,
    RecallInsufficientStorageError,
    build_create_bot_payload,
    region_base_url,
)

# Long, recognizable fake key so any leak is trivially asserted. Not real.
SECRET_VALUE = "example-api-value"
MEETING_URL = "https://meet.google.com/abc-defg-hij"


def _bot_created_body(bot_id: str = "bot_abc123") -> dict:
    """A minimal Recall create-bot 201 response body."""
    return {
        "id": bot_id,
        "status_changes": [
            {"code": "ready", "created_at": "2026-06-01T10:00:00Z"},
            {"code": "joining_call", "created_at": "2026-06-01T10:00:05Z"},
        ],
        "meeting_url": {"meeting_id": "abc-defg-hij", "platform": "google_meet"},
    }


class _Recorder:
    """A ``MockTransport`` handler that records requests and replies fixed."""

    def __init__(self, *, status_code: int = 201, json_body: dict | None = None):
        self.status_code = status_code
        self.json_body = json_body if json_body is not None else _bot_created_body()
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(self.status_code, json=self.json_body, request=request)

    @property
    def last(self) -> httpx.Request:
        assert self.requests, "no request was made"
        return self.requests[-1]

    @property
    def last_json(self) -> dict:
        return json.loads(self.last.content)


def _make_client(
    recorder: _Recorder,
    *,
    api_key: str = SECRET_VALUE,
    region: str = "us-east-1",
    base_url: str | None = None,
) -> RecallClient:
    return RecallClient(
        api_key,
        region=region,
        base_url=base_url,
        transport=httpx.MockTransport(recorder),
    )


# ── region → base URL ────────────────────────────────────────────────────────


def test_region_base_url_default():
    assert region_base_url("us-east-1") == "https://us-east-1.recall.ai"


def test_region_base_url_normalizes_case_and_whitespace():
    assert region_base_url("  EU-Central-1 ") == "https://eu-central-1.recall.ai"


def test_region_base_url_empty_falls_back_to_default():
    assert region_base_url("") == "https://us-east-1.recall.ai"


# ── URL construction ─────────────────────────────────────────────────────────


def test_create_bot_builds_default_region_url():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL)

    assert str(recorder.last.url) == "https://us-east-1.recall.ai/api/v1/bot/"


def test_create_bot_url_reflects_region():
    recorder = _Recorder()
    client = _make_client(recorder, region="eu-central-1")

    client.create_bot(MEETING_URL)

    assert str(recorder.last.url) == "https://eu-central-1.recall.ai/api/v1/bot/"


def test_create_bot_uses_post():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL)

    assert recorder.last.method == "POST"


def test_explicit_base_url_overrides_region():
    recorder = _Recorder()
    client = _make_client(recorder, base_url="https://recall.test", region="us-east-1")

    client.create_bot(MEETING_URL)

    assert str(recorder.last.url) == "https://recall.test/api/v1/bot/"


# ── Authorization header ─────────────────────────────────────────────────────


def test_authorization_header_uses_token_scheme_and_key():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL)

    assert recorder.last.headers["Authorization"] == f"Token {SECRET_VALUE}"


def test_accepts_json():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL)

    assert "application/json" in recorder.last.headers.get("Accept", "")


# ── Payload shape ────────────────────────────────────────────────────────────


def test_create_bot_payload_shape():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL, bot_name="Example Notetaker")

    body = recorder.last_json
    assert body["meeting_url"] == MEETING_URL
    assert body["bot_name"] == "Example Notetaker"

    transcript = body["recording_config"]["transcript"]
    streaming = transcript["provider"]["recallai_streaming"]
    assert streaming["mode"] == DEFAULT_TRANSCRIPT_MODE == "prioritize_accuracy"
    assert streaming["language_code"] == "auto"
    assert transcript["diarization"]["use_separate_streams_when_available"] is True


def test_create_bot_payload_includes_automatic_leave_and_metadata():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL, metadata={"requested_by": "alice", "topic": "bd"})

    body = recorder.last_json
    assert body["automatic_leave"]["waiting_room_timeout"] == 600
    # Caller metadata overrides defaults but is merged with them.
    assert body["metadata"]["source"] == "recall-meeting-assistant"
    assert body["metadata"]["requested_by"] == "alice"
    assert body["metadata"]["topic"] == "bd"


def test_build_create_bot_payload_standalone():
    payload = build_create_bot_payload(MEETING_URL, transcription_mode="prioritize_low_latency")
    assert payload["recording_config"]["transcript"]["provider"]["recallai_streaming"][
        "mode"
    ] == "prioritize_low_latency"


def test_create_bot_serializes_non_string_metadata_values():
    payload = build_create_bot_payload(
        MEETING_URL,
        metadata={
            "summary_recipient_emails": ["redacted@example.invalid", "redacted@example.invalid"],
            "enabled": True,
            "attempt": 2,
        },
    )

    assert payload["metadata"] == {
        "source": "recall-meeting-assistant",
        "summary_recipient_emails": '["redacted@example.invalid","redacted@example.invalid"]',
        "enabled": "true",
        "attempt": "2",
    }
    assert all(isinstance(value, str) for value in payload["metadata"].values())


def test_build_create_bot_payload_requires_meeting_url():
    with pytest.raises(ValueError):
        build_create_bot_payload("   ")


# ── Success path ─────────────────────────────────────────────────────────────


def test_create_bot_201_returns_bot_record():
    recorder = _Recorder(status_code=201, json_body=_bot_created_body("bot_xyz"))
    client = _make_client(recorder)

    bot = client.create_bot(MEETING_URL)

    assert isinstance(bot, RecallBot)
    assert bot.id == "bot_xyz"
    # Most recent status_changes entry wins.
    assert bot.status == "joining_call"
    assert bot.raw["meeting_url"]["platform"] == "google_meet"


def test_create_bot_makes_exactly_one_request():
    recorder = _Recorder()
    client = _make_client(recorder)

    client.create_bot(MEETING_URL)

    assert len(recorder.requests) == 1


def test_create_bot_response_missing_id_raises():
    recorder = _Recorder(status_code=201, json_body={"status_changes": []})
    client = _make_client(recorder)

    with pytest.raises(RecallAPIError):
        client.create_bot(MEETING_URL)


# ── Typed error handling ─────────────────────────────────────────────────────


def test_400_raises_bad_request_error():
    recorder = _Recorder(
        status_code=400, json_body={"meeting_url": ["Enter a valid URL."]}
    )
    client = _make_client(recorder)

    with pytest.raises(RecallBadRequestError) as exc_info:
        client.create_bot(MEETING_URL)

    assert exc_info.value.status_code == 400


def test_401_raises_auth_error():
    recorder = _Recorder(
        status_code=401, json_body={"detail": "Invalid token."}
    )
    client = _make_client(recorder)

    with pytest.raises(RecallAuthError) as exc_info:
        client.create_bot(MEETING_URL)

    assert exc_info.value.status_code == 401


def test_507_raises_insufficient_storage_error():
    recorder = _Recorder(
        status_code=507,
        json_body={"detail": "Bot concurrency limit reached."},
    )
    client = _make_client(recorder)

    with pytest.raises(RecallInsufficientStorageError) as exc_info:
        client.create_bot(MEETING_URL)

    assert exc_info.value.status_code == 507


def test_unmapped_status_raises_generic_api_error():
    recorder = _Recorder(status_code=503, json_body={"detail": "down"})
    client = _make_client(recorder)

    with pytest.raises(RecallAPIError) as exc_info:
        client.create_bot(MEETING_URL)

    # Generic base error, not one of the specific subclasses.
    assert type(exc_info.value) is RecallAPIError
    assert exc_info.value.status_code == 503


def test_typed_errors_are_recall_api_errors():
    for cls in (RecallBadRequestError, RecallAuthError, RecallInsufficientStorageError):
        assert issubclass(cls, RecallAPIError)


def test_error_handles_non_json_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="<html>Bad Request</html>", request=request)

    client = RecallClient(SECRET_VALUE, transport=httpx.MockTransport(handler))

    with pytest.raises(RecallBadRequestError) as exc_info:
        client.create_bot(MEETING_URL)
    assert exc_info.value.status_code == 400


# ── Retrieval endpoints used by post-meeting ingest ──────────────────────────


def test_get_bot_uses_retrieve_bot_endpoint():
    recorder = _Recorder(status_code=200, json_body={"id": "bot_123", "recordings": []})
    client = _make_client(recorder)

    payload = client.get_bot("bot_123")

    assert payload["id"] == "bot_123"
    assert recorder.last.method == "GET"
    assert str(recorder.last.url) == "https://us-east-1.recall.ai/api/v1/bot/bot_123/"


def test_get_recording_resource_retrieves_transcript_artifact():
    recorder = _Recorder(
        status_code=200,
        json_body={"id": "tr_123", "data": {"transcript": []}, "recording": {"id": "rec_123"}},
    )
    client = _make_client(recorder)

    payload = client.get_recording_resource("tr_123")

    assert payload["id"] == "tr_123"
    assert str(recorder.last.url) == "https://us-east-1.recall.ai/api/v1/transcript/tr_123/"


def test_get_recording_resource_rejects_unsupported_resource_type():
    client = _make_client(_Recorder())

    with pytest.raises(ValueError):
        client.get_recording_resource("vid_123", resource_type="video_mixed")


# ── Secret hygiene ───────────────────────────────────────────────────────────


def test_error_does_not_leak_api_key():
    recorder = _Recorder(status_code=401, json_body={"detail": "Invalid token."})
    client = _make_client(recorder, api_key=SECRET_VALUE)

    with pytest.raises(RecallAuthError) as exc_info:
        client.create_bot(MEETING_URL)

    assert SECRET_VALUE not in str(exc_info.value)
    assert SECRET_VALUE not in repr(exc_info.value)


def test_repr_does_not_leak_api_key():
    client = _make_client(_Recorder(), api_key=SECRET_VALUE)
    assert SECRET_VALUE not in repr(client)


def test_create_bot_does_not_print_secrets(capsys):
    recorder = _Recorder()
    client = _make_client(recorder, api_key=SECRET_VALUE)

    client.create_bot(MEETING_URL)

    captured = capsys.readouterr()
    assert SECRET_VALUE not in (captured.out + captured.err)


# ── from_config ──────────────────────────────────────────────────────────────


def test_from_config_uses_region_and_key():
    from recall_meeting_assistant.config import RecallMeetingsConfig

    cfg = RecallMeetingsConfig(
        recallai_api_key=SECRET_VALUE,
        recallai_webhook_secret="example-webhook-value",
        openai_api_key="example-provider-value",
        public_base_url="https://hooks.example.com/recall",
        recallai_region="eu-west-1",
    )
    recorder = _Recorder()
    client = RecallClient.from_config(cfg, transport=httpx.MockTransport(recorder))

    client.create_bot(MEETING_URL)

    assert str(recorder.last.url) == "https://eu-west-1.recall.ai/api/v1/bot/"
    assert recorder.last.headers["Authorization"] == f"Token {SECRET_VALUE}"
