"""Tests for the Recall.ai meeting-assistant webhook receiver/verifier.

Covers Task 4 of docs/architecture.md:

* Recall webhook requests are verified with HMAC SHA-256 over the *raw* body,
  or via a shared URL-token helper; both use constant-time comparison.
* Invalid signatures / tokens are rejected with a non-2xx result and a typed
  verification error path — no event is produced.
* The handler is fast: it parses + verifies + returns a small
  :class:`WebhookEvent` / :class:`QueuedWebhookResult` for the outer gateway
  queue.  Heavy work (transcript download) is *not* done inline.
* ``transcript.done``, ``transcript.failed`` and bot status-change events are
  recognised and normalised.
* No secret, raw body, or meeting URL is ever logged.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from recall_meeting_assistant.webhooks import (
    CATEGORY_BOT_STATUS,
    CATEGORY_TRANSCRIPT_DONE,
    CATEGORY_TRANSCRIPT_FAILED,
    CATEGORY_UNKNOWN,
    QueuedWebhookResult,
    WebhookEvent,
    WebhookVerificationError,
    handle_webhook,
    parse_event,
    sign_body,
    verify_signature,
    verify_url_token,
)

SECRET = "example-shared-value"
MEETING_URL = "https://meet.google.com/abc-defg-hij"


def _body(payload: dict) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _transcript_done_payload(
    *, bot_id: str = "bot_abc", transcript_id: str = "tr_123", recording_id: str = "rec_9"
) -> dict:
    return {
        "event": "transcript.done",
        "data": {
            "bot": {"id": bot_id, "metadata": {"source": "recall-meeting-assistant"}},
            "recording": {"id": recording_id},
            "transcript": {"id": transcript_id, "status": {"code": "done"}},
        },
    }


def _transcript_failed_payload() -> dict:
    return {
        "event": "transcript.failed",
        "data": {
            "bot": {"id": "bot_abc"},
            "recording": {"id": "rec_9"},
            "transcript": {"id": "tr_123", "status": {"code": "failed", "sub_code": "provider_error"}},
        },
    }


def _bot_status_payload(code: str = "in_call_recording") -> dict:
    return {
        "event": "bot.status_change",
        "data": {
            "bot": {"id": "bot_abc"},
            "status": {"code": code, "created_at": "2026-06-01T10:00:00Z"},
        },
    }


# ── raw-body HMAC signing / verification ─────────────────────────────────────


def test_sign_body_is_hex_hmac_sha256():
    body = _body({"hello": "world"})
    expected = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    assert sign_body(body, SECRET) == expected


def test_verify_signature_accepts_bare_hex():
    body = _body(_transcript_done_payload())
    assert verify_signature(body, sign_body(body, SECRET), SECRET) is True


def test_verify_signature_accepts_sha256_prefixed_hex():
    body = _body(_transcript_done_payload())
    sig = sign_body(body, SECRET)
    assert verify_signature(body, f"sha256={sig}", SECRET) is True


def test_verify_signature_accepts_base64_and_svix_style():
    body = _body(_transcript_done_payload())
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).digest()
    b64 = base64.b64encode(digest).decode()
    assert verify_signature(body, b64, SECRET) is True
    # Svix-style "v1,<base64>" (possibly space-separated multiple values).
    assert verify_signature(body, f"v1,{b64}", SECRET) is True
    assert verify_signature(body, f"v1,deadbeef v1,{b64}", SECRET) is True


def test_verify_signature_rejects_wrong_secret():
    body = _body(_transcript_done_payload())
    assert verify_signature(body, sign_body(body, SECRET), "other-example-value") is False


def test_verify_signature_rejects_tampered_body():
    body = _body(_transcript_done_payload())
    sig = sign_body(body, SECRET)
    assert verify_signature(body + b"x", sig, SECRET) is False


def test_verify_signature_rejects_empty_or_missing():
    body = _body(_transcript_done_payload())
    assert verify_signature(body, "", SECRET) is False
    assert verify_signature(body, None, SECRET) is False
    assert verify_signature(body, "deadbeef", "") is False


# ── shared URL-token verification ────────────────────────────────────────────


def test_verify_url_token_constant_time_match():
    assert verify_url_token("tok-12345", "tok-12345") is True


def test_verify_url_token_rejects_mismatch_empty_and_none():
    assert verify_url_token("tok-12345", "tok-12346") is False
    assert verify_url_token(None, "tok") is False
    assert verify_url_token("tok", "") is False
    assert verify_url_token("", "") is False


# ── parse_event normalisation ────────────────────────────────────────────────


def test_parse_event_transcript_done_extracts_ids():
    event = parse_event(_transcript_done_payload(bot_id="bot_x", transcript_id="tr_y", recording_id="rec_z"))
    assert isinstance(event, WebhookEvent)
    assert event.event_type == "transcript.done"
    assert event.category == CATEGORY_TRANSCRIPT_DONE
    assert event.bot_id == "bot_x"
    assert event.transcript_id == "tr_y"
    assert event.recording_id == "rec_z"


def test_parse_event_transcript_failed_captures_reason():
    event = parse_event(_transcript_failed_payload())
    assert event.category == CATEGORY_TRANSCRIPT_FAILED
    assert event.transcript_id == "tr_123"
    assert event.failure_reason == "provider_error"


def test_parse_event_bot_status_captures_code():
    event = parse_event(_bot_status_payload("in_call_recording"))
    assert event.category == CATEGORY_BOT_STATUS
    assert event.bot_id == "bot_abc"
    assert event.status_code == "in_call_recording"


def test_parse_event_handles_flat_bot_id_and_transcript_id_shapes():
    payload = {
        "event": "transcript.done",
        "data": {"bot_id": "bot_flat", "transcript_id": "tr_flat", "recording_id": "rec_flat"},
    }
    event = parse_event(payload)
    assert event.bot_id == "bot_flat"
    assert event.transcript_id == "tr_flat"
    assert event.recording_id == "rec_flat"


def test_parse_event_unknown_event_type():
    event = parse_event({"event": "calendar.sync", "data": {}})
    assert event.category == CATEGORY_UNKNOWN


def test_parse_event_accepts_raw_bytes_and_str():
    raw = _body(_transcript_done_payload())
    assert parse_event(raw).category == CATEGORY_TRANSCRIPT_DONE
    assert parse_event(raw.decode()).category == CATEGORY_TRANSCRIPT_DONE


# ── handle_webhook: verification gating ──────────────────────────────────────


def test_handle_webhook_accepts_valid_signature_and_queues():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert isinstance(result, QueuedWebhookResult)
    assert result.accepted is True
    assert 200 <= result.status_code < 300
    assert result.should_enqueue is True
    assert result.event is not None
    assert result.event.category == CATEGORY_TRANSCRIPT_DONE


def test_handle_webhook_rejects_invalid_signature():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET, signature="sha256=deadbeef")
    assert result.accepted is False
    assert result.status_code == 401
    assert result.event is None
    assert result.should_enqueue is False


def test_handle_webhook_rejects_when_no_credentials_supplied():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET)
    assert result.accepted is False
    assert result.status_code == 401


def test_handle_webhook_accepts_valid_url_token():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET, url_token=SECRET)
    assert result.accepted is True
    assert 200 <= result.status_code < 300


def test_handle_webhook_rejects_invalid_url_token():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET, url_token="not-the-token")
    assert result.accepted is False
    assert result.status_code == 401


def test_handle_webhook_signature_takes_precedence_over_token():
    body = _body(_transcript_done_payload())
    # A bad signature must reject even if a (would-be) valid token is present.
    result = handle_webhook(
        body, secret=SECRET, signature="sha256=deadbeef", url_token=SECRET
    )
    assert result.accepted is False
    assert result.status_code == 401


# ── handle_webhook: event categories ─────────────────────────────────────────


def test_handle_webhook_transcript_failed_enqueues_for_fallback():
    body = _body(_transcript_failed_payload())
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert result.accepted is True
    assert result.event.category == CATEGORY_TRANSCRIPT_FAILED
    assert result.should_enqueue is True


def test_handle_webhook_bot_status_accepted():
    body = _body(_bot_status_payload("joining_call"))
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert result.accepted is True
    assert result.event.category == CATEGORY_BOT_STATUS
    assert result.event.status_code == "joining_call"


def test_handle_webhook_unknown_event_accepted_but_not_enqueued():
    body = _body({"event": "calendar.sync", "data": {}})
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert result.accepted is True
    assert 200 <= result.status_code < 300
    assert result.event.category == CATEGORY_UNKNOWN
    assert result.should_enqueue is False


def test_handle_webhook_malformed_json_with_valid_signature_is_bad_request():
    body = b"{not valid json"
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert result.accepted is False
    assert result.status_code == 400
    assert result.event is None


def test_handle_webhook_raise_on_invalid_raises_typed_error():
    body = _body(_transcript_done_payload())
    with pytest.raises(WebhookVerificationError):
        handle_webhook(body, secret=SECRET, signature="sha256=deadbeef", raise_on_invalid=True)


# ── secret / body / URL hygiene ──────────────────────────────────────────────


def test_handle_webhook_does_not_log_secret_or_body(caplog):
    caplog.set_level("DEBUG")
    payload = _transcript_done_payload()
    payload["data"]["bot"]["meeting_url"] = MEETING_URL
    body = _body(payload)
    handle_webhook(body, secret=SECRET, signature="sha256=deadbeef")  # rejected path
    handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))  # accepted path
    logged = caplog.text
    assert SECRET not in logged
    assert MEETING_URL not in logged
    assert "abc-defg-hij" not in logged


def test_result_repr_does_not_leak_secret_or_body():
    body = _body(_transcript_done_payload())
    result = handle_webhook(body, secret=SECRET, signature=sign_body(body, SECRET))
    assert SECRET not in repr(result)


def test_package_exports_webhook_symbols():
    import recall_meeting_assistant as pkg

    assert hasattr(pkg, "handle_webhook")
    assert hasattr(pkg, "WebhookEvent")
