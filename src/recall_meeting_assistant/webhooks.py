"""Webhook receiver / verifier for the Recall.ai meeting-assistant package.

Task 4 of docs/architecture.md.  This module is
deliberately a *thin, fast, transport-agnostic* library: it verifies an
incoming Recall webhook, parses it into a small :class:`WebhookEvent`, and
returns a :class:`QueuedWebhookResult` describing what the surrounding
receiver should do (return a status code, and whether to enqueue heavier work
like transcript download). It performs **no** network I/O itself, so the HTTP
route can verify + return a 2xx quickly and process the artifact off the request
path.

Two invariants hold here:

1. **Authenticity is verified before anything else.**  Recall signs the *raw*
   request body with HMAC-SHA256; we recompute and compare in constant time
   (:func:`verify_signature`).  A shared URL-token scheme is also supported for
   deployments that prefer a per-endpoint secret query param
   (:func:`verify_url_token`).  Either way the comparison is constant-time and
   an invalid request never yields an event.
2. **No secrets, raw bodies, or meeting URLs are logged.**  Only non-secret,
   coarse facts (event category, accept/reject) ever reach the logger, and the
   :class:`QueuedWebhookResult` ``repr`` omits the raw payload.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# ── Normalised event categories ──────────────────────────────────────────────
CATEGORY_TRANSCRIPT_DONE = "transcript_done"
CATEGORY_TRANSCRIPT_FAILED = "transcript_failed"
CATEGORY_BOT_STATUS = "bot_status"
CATEGORY_UNKNOWN = "unknown"

#: Bot status codes that mean the meeting is over — worth enqueuing follow-up
#: work (e.g. confirming transcript availability) even absent a transcript event.
_TERMINAL_BOT_STATUS = frozenset({"done", "fatal", "call_ended", "recording_done"})


# ── Errors ───────────────────────────────────────────────────────────────────
class WebhookError(ValueError):
    """Base class for webhook ingestion errors."""


class WebhookVerificationError(WebhookError):
    """Raised when a webhook's signature / token fails verification.

    Carries no secret material — only a short, non-secret ``reason`` code.
    """

    def __init__(self, reason: str = "invalid_signature") -> None:
        super().__init__(reason)
        self.reason = reason


class WebhookPayloadError(WebhookError):
    """Raised when a (verified) webhook body is not valid JSON we can parse."""


# ── Signature helpers ────────────────────────────────────────────────────────
def _as_bytes(raw_body: Any) -> bytes:
    if isinstance(raw_body, bytes):
        return raw_body
    if isinstance(raw_body, bytearray):
        return bytes(raw_body)
    if isinstance(raw_body, str):
        return raw_body.encode("utf-8")
    raise TypeError("raw_body must be bytes or str.")


def sign_body(raw_body: bytes | str, secret: str) -> str:
    """Return the hex HMAC-SHA256 of ``raw_body`` keyed by ``secret``.

    This is the canonical signature Recall sends (and that we recompute to
    verify).  Exposed so the dev CLI / tests can craft signed requests without
    duplicating the construction.
    """
    return hmac.new(secret.encode("utf-8"), _as_bytes(raw_body), hashlib.sha256).hexdigest()


def _candidate_signatures(provided: str) -> list[str]:
    """Split a signature header into bare candidate values.

    Handles the common shapes seen in the wild:

    * GitHub-style ``sha256=<hex>``
    * Svix/Recall-style ``v1,<base64>`` (possibly several space-separated)
    * a bare hex or base64 digest
    """
    candidates: list[str] = []
    for token in provided.replace(",", " ").split():
        value = token.strip()
        if not value:
            continue
        # Strip an ``algo=`` scheme prefix (``sha256=``) if present.
        if "=" in value and value.split("=", 1)[0].lower() in {"sha256", "sha1", "hmac"}:
            value = value.split("=", 1)[1]
        candidates.append(value)
    return candidates


def verify_signature(
    raw_body: bytes | str | None, provided_signature: str | None, secret: str | None
) -> bool:
    """Constant-time HMAC-SHA256 verification of ``raw_body``.

    Returns ``True`` only when ``provided_signature`` matches the HMAC of the
    *exact* raw body keyed by ``secret``.  Accepts the digest as bare hex,
    ``sha256=<hex>``, base64, or Svix-style ``v1,<base64>``.  Any missing input
    returns ``False`` (never raises) so callers can branch on the boolean.
    """
    if not provided_signature or not secret or raw_body is None:
        return False

    body = _as_bytes(raw_body)
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).digest()
    expected_hex = digest.hex()
    expected_b64 = base64.b64encode(digest).decode("ascii")
    expected_b64_unpadded = expected_b64.rstrip("=")

    matched = False
    for candidate in _candidate_signatures(provided_signature):
        # Compare against every accepted encoding; keep scanning every
        # candidate so timing does not reveal *which* form/position matched.
        for expected in (expected_hex, expected_b64, expected_b64_unpadded):
            if len(candidate) == len(expected) and hmac.compare_digest(candidate, expected):
                matched = True
    return matched


def verify_url_token(provided_token: str | None, expected_token: str | None) -> bool:
    """Constant-time comparison of a shared URL token.

    For deployments that authenticate the webhook endpoint with a secret query
    param / path segment instead of (or in addition to) a body signature.
    Empty / missing values always fail.
    """
    if not provided_token or not expected_token:
        return False
    return hmac.compare_digest(provided_token.encode("utf-8"), expected_token.encode("utf-8"))


# ── Parsed event ─────────────────────────────────────────────────────────────
@dataclass
class WebhookEvent:
    """A verified, normalised Recall webhook event.

    ``category`` is one of the ``CATEGORY_*`` constants; ``raw`` is the full
    decoded payload for downstream handlers that need provider-specific fields.
    """

    event_type: str
    category: str
    bot_id: str | None = None
    recording_id: str | None = None
    transcript_id: str | None = None
    status_code: str | None = None
    failure_reason: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _get(mapping: Any, key: str) -> Any:
    return mapping.get(key) if isinstance(mapping, dict) else None


def _categorize(event_type: str) -> str:
    lowered = event_type.lower()
    if lowered in {"transcript.done", "transcript.completed", "transcript_done"}:
        return CATEGORY_TRANSCRIPT_DONE
    if lowered in {"transcript.failed", "transcript.error", "transcript_failed"}:
        return CATEGORY_TRANSCRIPT_FAILED
    if lowered.startswith("bot.") or lowered in {"bot_status_change", "bot.status_change"}:
        return CATEGORY_BOT_STATUS
    return CATEGORY_UNKNOWN


def parse_event(payload: dict[str, Any] | bytes | str) -> WebhookEvent:
    """Normalise a Recall webhook payload into a :class:`WebhookEvent`.

    Tolerant of the several payload shapes Recall has shipped: ids may live
    under nested objects (``data.bot.id``) or flat keys (``data.bot_id``); the
    status code under ``data.status.code`` or ``data.data.code``.
    """
    if isinstance(payload, (bytes, bytearray, str)):
        payload = json.loads(_as_bytes(payload).decode("utf-8"))
    if not isinstance(payload, dict):
        raise WebhookPayloadError("Webhook payload must be a JSON object.")

    event_type = _first_str(payload.get("event"), payload.get("type")) or ""
    category = _categorize(event_type)
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}

    bot = _get(data, "bot")
    recording = _get(data, "recording")
    transcript = _get(data, "transcript")
    status = _get(data, "status") or _get(data, "data")

    bot_id = _first_str(_get(bot, "id"), _get(data, "bot_id"), payload.get("bot_id"))
    recording_id = _first_str(_get(recording, "id"), _get(data, "recording_id"))
    transcript_id = _first_str(_get(transcript, "id"), _get(data, "transcript_id"))

    status_code = None
    if category == CATEGORY_BOT_STATUS:
        status_code = _first_str(_get(status, "code"), _get(data, "code"))

    failure_reason = None
    if category == CATEGORY_TRANSCRIPT_FAILED:
        transcript_status = _get(transcript, "status")
        failure_reason = _first_str(
            _get(transcript_status, "sub_code"),
            _get(transcript_status, "code"),
            _get(status, "sub_code"),
            _get(status, "code"),
            _get(data, "code"),
        )

    return WebhookEvent(
        event_type=event_type,
        category=category,
        bot_id=bot_id,
        recording_id=recording_id,
        transcript_id=transcript_id,
        status_code=status_code,
        failure_reason=failure_reason,
        raw=payload,
    )


# ── Queue result ─────────────────────────────────────────────────────────────
@dataclass
class QueuedWebhookResult:
    """The handler's verdict for the outer gateway queue.

    ``status_code`` is the HTTP status the gateway should return (always a 2xx
    for accepted requests, so Recall does not retry); ``should_enqueue`` tells
    the gateway whether heavier off-request work (transcript download / fallback)
    is warranted.  ``reason`` is a short, non-secret code for observability.
    """

    accepted: bool
    status_code: int
    reason: str
    should_enqueue: bool = False
    event: WebhookEvent | None = None

    def __repr__(self) -> str:  # keep the raw payload out of logs/reprs
        category = self.event.category if self.event else None
        return (
            f"{type(self).__name__}(accepted={self.accepted}, "
            f"status_code={self.status_code}, reason={self.reason!r}, "
            f"should_enqueue={self.should_enqueue}, category={category!r})"
        )


def _should_enqueue(event: WebhookEvent) -> bool:
    if event.category in (CATEGORY_TRANSCRIPT_DONE, CATEGORY_TRANSCRIPT_FAILED):
        return True
    if event.category == CATEGORY_BOT_STATUS and event.status_code:
        return event.status_code.lower() in _TERMINAL_BOT_STATUS
    return False


def handle_webhook(
    raw_body: bytes | str,
    *,
    secret: str,
    signature: str | None = None,
    url_token: str | None = None,
    raise_on_invalid: bool = False,
) -> QueuedWebhookResult:
    """Verify + parse a Recall webhook and return a queue verdict.

    Verification order is deterministic: if ``signature`` is supplied it must
    validate against the raw-body HMAC; otherwise, if ``url_token`` is supplied
    it must match the shared token; if neither is supplied the request is
    rejected.  A signature always takes precedence over a token so that a
    correct token cannot rescue a forged body.

    On an invalid request returns ``accepted=False`` with HTTP 401 (or raises
    :class:`WebhookVerificationError` when ``raise_on_invalid``).  On a verified
    but unparseable body returns HTTP 400.  Never logs the secret, raw body, or
    meeting URL.
    """
    body = _as_bytes(raw_body)

    if signature is not None:
        valid = verify_signature(body, signature, secret)
    elif url_token is not None:
        valid = verify_url_token(url_token, secret)
    else:
        valid = False

    if not valid:
        logger.warning("Recall webhook rejected: failed verification")
        if raise_on_invalid:
            raise WebhookVerificationError("invalid_signature")
        return QueuedWebhookResult(
            accepted=False, status_code=401, reason="invalid_signature"
        )

    try:
        event = parse_event(body)
    except (WebhookPayloadError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        logger.warning("Recall webhook accepted but body was unparseable")
        return QueuedWebhookResult(
            accepted=False, status_code=400, reason="invalid_payload"
        )

    should_enqueue = _should_enqueue(event)
    # 202 when there is follow-up work to queue, 200 for accept-and-ignore.
    status_code = 202 if should_enqueue else 200
    logger.info(
        "Recall webhook accepted: category=%s enqueue=%s", event.category, should_enqueue
    )
    return QueuedWebhookResult(
        accepted=True,
        status_code=status_code,
        reason=event.category,
        should_enqueue=should_enqueue,
        event=event,
    )


__all__ = [
    "CATEGORY_TRANSCRIPT_DONE",
    "CATEGORY_TRANSCRIPT_FAILED",
    "CATEGORY_BOT_STATUS",
    "CATEGORY_UNKNOWN",
    "WebhookError",
    "WebhookVerificationError",
    "WebhookPayloadError",
    "WebhookEvent",
    "QueuedWebhookResult",
    "sign_body",
    "verify_signature",
    "verify_url_token",
    "parse_event",
    "handle_webhook",
]
