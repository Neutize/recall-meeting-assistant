from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from pathlib import Path

from recall_meeting_assistant.receiver import (
    Handler,
    choose_secret,
    store_event,
    verify_recall_signature,
    webhook_store_dir,
)

TEST_SECRET = "whsec_" + base64.b64encode(b"test").decode()


def _headers(body: bytes, *, timestamp: int = 1_700_000_000) -> dict[str, str]:
    message_id = "msg_example"
    signed = f"{message_id}.{timestamp}.{body.decode()}".encode()
    digest = hmac.new(base64.b64decode("dGVzdA=="), signed, hashlib.sha256).digest()
    signature = base64.b64encode(digest).decode()
    return {
        "webhook-id": message_id,
        "webhook-timestamp": str(timestamp),
        "webhook-signature": f"v1,{signature}",
    }


def test_choose_secret_accepts_only_svix_format():
    assert choose_secret({"RECALLAI_WEBHOOK_SECRET": TEST_SECRET}) == TEST_SECRET
    assert choose_secret({"RECALLAI_WEBHOOK_SECRET": "example-value"}) is None


def test_verify_recall_signature_accepts_valid_and_rejects_tampered_body():
    body = b'{"event":"transcript.done"}'
    headers = _headers(body)
    assert verify_recall_signature(headers, body, TEST_SECRET, now=1_700_000_001) == (True, "ok")
    assert verify_recall_signature(headers, body + b" ", TEST_SECRET, now=1_700_000_001)[0] is False


def test_verify_recall_signature_rejects_stale_messages():
    body = b"{}"
    headers = _headers(body, timestamp=1_700_000_000)
    ok, reason = verify_recall_signature(headers, body, TEST_SECRET, now=1_700_100_000)
    assert ok is False
    assert reason == "timestamp_outside_tolerance"


def test_store_event_persists_only_safe_metadata_and_private_body(tmp_path: Path):
    body = b'{"event":"transcript.done","data":{"id":"example"}}'
    path = store_event(_headers(body), body, store_dir=tmp_path)
    document = json.loads(path.read_text())
    assert document["event"] == "transcript.done"
    assert document["body"] == {"event": "transcript.done", "data": {"id": "example"}}
    assert "webhook-signature" not in document["headers"]
    assert path.stat().st_mode & 0o077 == 0


def test_webhook_store_dir_uses_configured_storage(tmp_path: Path):
    assert webhook_store_dir({"MEETING_ASSISTANT_STORAGE_DIR": str(tmp_path)}) == tmp_path / "webhooks"


def test_request_logging_redacts_query_tokens(caplog):
    handler = Handler.__new__(Handler)
    handler.path = "/webhooks/recall-ai?token=example-query-token"
    with caplog.at_level(logging.INFO, logger="recall_meeting_assistant.receiver"):
        handler.log_message('"POST %s HTTP/1.1" 204 -', handler.path)
    assert "example-query-token" not in caplog.text
    assert "/webhooks/recall-ai" in caplog.text
