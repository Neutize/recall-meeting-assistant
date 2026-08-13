"""Post-meeting ingest automation for Recall.ai transcript completion.

Phase 1 closes here: once the webhook layer has accepted a Recall event, this
module performs the heavier off-request work of fetching the finished transcript,
normalizing it, persisting private artifacts, and invoking the OpenAI fallback
when quality gates say the primary transcript is unusable.

The module is intentionally dependency-injected. Tests pass fake clients and
fallback callables, so no real Recall/OpenAI network calls happen unless the
operator wires real dependencies in production.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol
from urllib.parse import urlparse

from recall_meeting_assistant.models import MeetingSession, redact_meeting_url
from recall_meeting_assistant.openai_fallback import FallbackResult
from recall_meeting_assistant.outbox import (
    DEFAULT_LEFT_MEETING_TEXT,
    queue_left_meeting_notification,
    queue_transcript_notification,
)
from recall_meeting_assistant.participants import ParticipantDirectory
from recall_meeting_assistant.recall_summary import ingest_recall_summary
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.transcript import (
    DEFAULT_PROVIDER as RECALL_TRANSCRIPT_PROVIDER,
)
from recall_meeting_assistant.transcript import (
    assess_transcript_quality,
    normalize_transcript,
    transcript_to_json,
    transcript_to_markdown,
    transcript_to_telegram,
)
from recall_meeting_assistant.webhooks import (
    CATEGORY_BOT_STATUS,
    CATEGORY_TRANSCRIPT_DONE,
    CATEGORY_TRANSCRIPT_FAILED,
    WebhookEvent,
)

logger = logging.getLogger(__name__)

RAW_BOT_JSON = "raw_bot.json"
RAW_TRANSCRIPT_RESOURCE_JSON = "raw_transcript_resource.json"
RAW_TRANSCRIPT_JSON = "raw_transcript.json"
NORMALIZED_TRANSCRIPT_JSON = "normalized_transcript.json"
TRANSCRIPT_MARKDOWN = "transcript.md"
OUTBOX_LEFT_MEETING_KEY = "outbox_left_meeting_json"
OUTBOX_TRANSCRIPT_KEY = "outbox_transcript_json"

_TERMINAL_LEFT_BOT_STATUSES = frozenset({"call_ended", "done", "fatal"})

_MEET_URL_RE = re.compile(r"https?://meet\.google\.com/[^\s)>'\"]+", re.IGNORECASE)
_URL_RE = re.compile(r"https?://[^\s)>'\"]+", re.IGNORECASE)
_SENSITIVE_QUERY_RE = re.compile(
    r"(?i)(X-Amz-[A-Za-z0-9_-]+|Signature|Expires|token|access_token|download_token|api_key)="
)


class RecallResourceClient(Protocol):
    """Minimal Recall client protocol required by the ingest flow."""

    def get_recording_resource(
        self,
        resource_id: str,
        *,
        resource_type: str = "transcript_artifact",
        include_transcript: bool = True,
    ) -> Mapping[str, Any]: ...

    def get_bot(self, bot_id: str) -> Mapping[str, Any]: ...


class IngestStatus(StrEnum):
    NOOP = "noop"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IngestResult:
    """Safe status object for post-webhook transcript ingest."""

    status: IngestStatus
    reason: str
    meeting_id: str | None = None
    provider: str | None = None
    fallback_used: bool = False
    fallback_reason: str | None = None
    artifact_paths: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def __repr__(self) -> str:
        return (
            f"IngestResult(status={self.status!s}, reason={self.reason!r}, "
            f"meeting_id={self.meeting_id!r}, provider={self.provider!r}, "
            f"fallback_used={self.fallback_used}, fallback_reason={self.fallback_reason!r}, "
            f"artifact_keys={sorted(self.artifact_paths)})"
        )


def redact_status_value(value: Any) -> str:
    """Return a log/status-safe string with access-granting URLs redacted."""
    text = str(value or "")
    if not text:
        return text

    def redact_meet(match: re.Match[str]) -> str:
        return redact_meeting_url(match.group(0)) or "***"

    text = _MEET_URL_RE.sub(redact_meet, text)

    def redact_url(match: re.Match[str]) -> str:
        url = match.group(0)
        parsed = urlparse(url)
        if parsed.query and _SENSITIVE_QUERY_RE.search(parsed.query):
            scheme = parsed.scheme or "https"
            host = parsed.netloc or "url"
            return f"{scheme}://{host}/***"
        return url

    return _URL_RE.sub(redact_url, text)


def _first_str(*values: Any) -> str | None:
    for value in values:
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _nested(mapping: Any, *keys: str) -> Any:
    value = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _extract_transcript_payload(resource: Mapping[str, Any]) -> Any:
    for key in ("transcript", "segments", "results", "monologues", "utterances"):
        value = resource.get(key)
        if value is not None:
            return value
    data = resource.get("data")
    if isinstance(data, Mapping):
        for key in ("transcript", "segments", "results", "monologues", "utterances"):
            value = data.get(key)
            if value is not None:
                return value
        download_url = _first_str(data.get("download_url"), data.get("provider_data_download_url"))
        if download_url:
            return _download_transcript_payload(download_url)
    return []


def _download_transcript_payload(download_url: str) -> Any:
    """Fetch the JSON transcript behind Recall's signed artifact URL.

    ``GET /api/v1/transcript/{id}/`` returns metadata plus short-lived signed
    URLs, not the word payload itself.  Keep the URL out of logs/errors and
    only surface a redacted, typed failure if download/parsing fails.
    """
    try:
        with urllib.request.urlopen(download_url, timeout=30) as response:  # noqa: S310 - signed Recall artifact URL
            body = response.read()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"transcript_download_failed: {_safe_error(exc)}") from exc
    try:
        return json.loads(body.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("transcript_download_invalid_json") from exc


def _extract_recording_id(event: WebhookEvent, resource: Mapping[str, Any]) -> str | None:
    return _first_str(
        event.recording_id,
        _nested(resource, "recording", "id"),
        resource.get("recording_id"),
    )


def _meeting_id_from(event: WebhookEvent, transcript_id: str | None) -> str:
    seed = _first_str(event.bot_id, transcript_id, event.recording_id) or "unknown"
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", seed).strip("_") or "unknown"
    return f"recall_{safe}"


def _write_json(store: MeetingStore, meeting_id: str, filename: str, payload: Any) -> str:
    path = store.artifact_path(meeting_id, filename, create_parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    return str(path)


def _write_text(store: MeetingStore, meeting_id: str, filename: str, text: str) -> str:
    path = store.artifact_path(meeting_id, filename, create_parents=True)
    path.write_text(text)
    return str(path)


def _safe_error(exc: Exception) -> str:
    """Return a retry-safe error code without echoing provider exception text."""

    return f"{type(exc).__name__}: ingest_failed"


def _save_session(
    store: MeetingStore,
    *,
    meeting_id: str,
    event: WebhookEvent,
    resource: Mapping[str, Any],
    bot_payload: Mapping[str, Any] | None,
    transcript_status: str,
    fallback_used: bool,
    fallback_reason: str | None,
) -> MeetingSession:
    existing = store.get_session(meeting_id)
    meeting_url_redacted = _first_str(
        existing.meeting_url_redacted if existing else None,
        redact_meeting_url(_first_str(_nested(bot_payload, "meeting_url"), _nested(resource, "meeting_url"))),
    )
    session = existing or MeetingSession(id=meeting_id)
    session.recall_bot_id = _first_str(event.bot_id, session.recall_bot_id)
    session.recall_recording_id = _first_str(_extract_recording_id(event, resource), session.recall_recording_id)
    session.recall_transcript_id = _first_str(event.transcript_id, session.recall_transcript_id)
    session.meeting_url_redacted = meeting_url_redacted
    session.status = "done"
    session.transcript_status = transcript_status
    session.fallback_used = fallback_used
    session.fallback_reason = fallback_reason
    session.artifact_dir = str(store.artifact_path(meeting_id))
    return store.create_session(session)


def _process_terminal_bot_status(
    event: WebhookEvent,
    *,
    client: RecallResourceClient,
    store: MeetingStore,
    delivery_backend: str,
    delivery_chat_id: str | None,
    delivery_thread_id: str | None,
    meeting_left_text: str,
) -> IngestResult:
    """Persist terminal bot status and queue a safe Telegram outbox notification."""

    status_code = (event.status_code or "").lower()
    if status_code not in _TERMINAL_LEFT_BOT_STATUSES:
        return IngestResult(status=IngestStatus.NOOP, reason="unsupported_bot_status")

    meeting_id = _meeting_id_from(event, None)
    artifact_paths: dict[str, str] = {}
    errors: list[str] = []
    bot_payload: Mapping[str, Any] | None = None
    try:
        if event.bot_id:
            bot_payload = client.get_bot(event.bot_id)
            artifact_paths["raw_bot_json"] = _write_json(store, meeting_id, RAW_BOT_JSON, bot_payload)
    except Exception as exc:  # noqa: BLE001 - never surface raw provider error material
        error = _safe_error(exc)
        logger.warning("Recall terminal bot status fetch failed: %s", error)
        errors.append(error)

    session = store.get_session(meeting_id) or MeetingSession(id=meeting_id)
    session.recall_bot_id = _first_str(event.bot_id, session.recall_bot_id)
    session.recall_recording_id = _first_str(
        _nested(bot_payload, "recording", "id"),
        _nested(bot_payload, "recording", "recording_id"),
        session.recall_recording_id,
    )
    session.recall_transcript_id = _first_str(
        _nested(bot_payload, "transcript", "id"),
        session.recall_transcript_id,
    )
    session.meeting_url_redacted = _first_str(
        session.meeting_url_redacted,
        redact_meeting_url(_first_str(_nested(bot_payload, "meeting_url"))),
    )
    session.status = "left" if status_code == "call_ended" else status_code
    session.summary_status = "pending"
    session.telegram_delivery_status = "queued"
    session.artifact_dir = str(store.artifact_path(meeting_id))
    store.create_session(session)

    artifact_paths[OUTBOX_LEFT_MEETING_KEY] = queue_left_meeting_notification(
        store,
        meeting_id=meeting_id,
        text=meeting_left_text,
        backend=delivery_backend,
        chat_id=delivery_chat_id,
        thread_id=delivery_thread_id,
    )
    logger.info(
        "Recall terminal bot status handled: meeting_id=%s status=%s notification=queued",
        meeting_id,
        status_code,
    )
    return IngestResult(
        status=IngestStatus.COMPLETED,
        reason="meeting_left_notification_queued",
        meeting_id=meeting_id,
        artifact_paths=artifact_paths,
        errors=errors,
    )


def process_recall_webhook(
    event: WebhookEvent,
    *,
    client: RecallResourceClient,
    store: MeetingStore,
    fallback_transcriber: Callable[..., FallbackResult] | Any | None = None,
    fallback_audio: bytes | bytearray | str | Path | None = None,
    fallback_policy: str = "auto",
    fallback_model: str | None = None,
    participant_count: int | None = None,
    meeting_duration_minutes: float | None = None,
    delivery_backend: str = "telegram",
    delivery_chat_id: str | None = None,
    delivery_thread_id: str | None = None,
    meeting_left_text: str = DEFAULT_LEFT_MEETING_TEXT,
    participant_directory: ParticipantDirectory | None = None,
    summary_recipient_emails: Iterable[str] | None = None,
) -> IngestResult:
    """Process a verified Recall webhook event into persisted transcript artifacts.

    ``transcript.done`` fetches and persists transcript artifacts. ``transcript.failed``
    can run fallback when fallback audio/transcriber are supplied. Terminal bot
    status events queue the user-facing "left meeting" outbox notification so
    a configured delivery adapter can notify the destination before optional
    summary generation starts.
    """
    if event.category == CATEGORY_BOT_STATUS:
        return _process_terminal_bot_status(
            event,
            client=client,
            store=store,
            delivery_backend=delivery_backend,
            delivery_chat_id=delivery_chat_id,
            delivery_thread_id=delivery_thread_id,
            meeting_left_text=meeting_left_text,
        )

    if event.category not in {CATEGORY_TRANSCRIPT_DONE, CATEGORY_TRANSCRIPT_FAILED}:
        return IngestResult(status=IngestStatus.NOOP, reason="unsupported_event")

    transcript_id = event.transcript_id
    if event.category == CATEGORY_TRANSCRIPT_DONE and not transcript_id:
        return IngestResult(status=IngestStatus.FAILED, reason="missing_transcript_id", errors=["missing_transcript_id"])

    meeting_id = _meeting_id_from(event, transcript_id)
    artifact_paths: dict[str, str] = {}
    errors: list[str] = []
    resource: Mapping[str, Any] = {}
    bot_payload: Mapping[str, Any] | None = None
    raw_transcript: Any = []

    try:
        if event.category == CATEGORY_TRANSCRIPT_DONE and transcript_id:
            resource = client.get_recording_resource(
                transcript_id,
                resource_type="transcript_artifact",
                include_transcript=True,
            )
            raw_transcript = _extract_transcript_payload(resource)
            artifact_paths["raw_transcript_resource_json"] = _write_json(
                store, meeting_id, RAW_TRANSCRIPT_RESOURCE_JSON, resource
            )
            artifact_paths["raw_transcript_json"] = _write_json(
                store, meeting_id, RAW_TRANSCRIPT_JSON, raw_transcript
            )
        if event.bot_id:
            bot_payload = client.get_bot(event.bot_id)
            artifact_paths["raw_bot_json"] = _write_json(store, meeting_id, RAW_BOT_JSON, bot_payload)
    except Exception as exc:  # noqa: BLE001 - structured failure, no raw secrets
        error = _safe_error(exc)
        logger.warning("Recall post-webhook ingest fetch failed: %s", error)
        errors.append(error)
        _save_session(
            store,
            meeting_id=meeting_id,
            event=event,
            resource=resource,
            bot_payload=bot_payload,
            transcript_status="failed",
            fallback_used=False,
            fallback_reason="download_failure",
        )
        return IngestResult(
            status=IngestStatus.FAILED,
            reason="download_failure",
            meeting_id=meeting_id,
            fallback_reason="download_failure",
            artifact_paths=artifact_paths,
            errors=errors,
        )

    provider_failed = event.category == CATEGORY_TRANSCRIPT_FAILED
    segments = normalize_transcript(raw_transcript, meeting_id=meeting_id, provider=RECALL_TRANSCRIPT_PROVIDER)
    quality = assess_transcript_quality(
        segments,
        participant_count=participant_count,
        meeting_duration_minutes=meeting_duration_minutes,
        provider_failed=provider_failed,
    )

    fallback_used = False
    fallback_reason = ",".join(quality.reasons) if quality.reasons else None
    provider = RECALL_TRANSCRIPT_PROVIDER
    final_segments = segments

    should_fallback = fallback_policy == "always" or (fallback_policy == "auto" and quality.should_fallback)
    if should_fallback:
        if fallback_transcriber is not None and fallback_audio is not None:
            trigger_reason = quality.reasons[0] if quality.reasons else "forced"
            if isinstance(fallback_transcriber, FallbackResult):
                fallback_result = fallback_transcriber
            elif fallback_model:
                fallback_result = fallback_transcriber(
                    meeting_id=meeting_id,
                    audio=fallback_audio,
                    trigger_reason=trigger_reason,
                    model=fallback_model,
                )
            else:
                fallback_result = fallback_transcriber(
                    meeting_id=meeting_id,
                    audio=fallback_audio,
                    trigger_reason=trigger_reason,
                )
            fallback_used = True
            fallback_reason = fallback_result.trigger_reason or trigger_reason
            if fallback_result.success and fallback_result.segments:
                final_segments = fallback_result.segments
                provider = fallback_result.provider
            else:
                errors.append(fallback_result.failure_reason or "fallback_failed")
        elif fallback_policy != "never":
            errors.append("fallback_unavailable")

    normalized = transcript_to_json(final_segments, meeting_id=meeting_id, provider=provider)
    markdown = transcript_to_markdown(final_segments, title="Recall Meeting Transcript")
    telegram_transcript = transcript_to_telegram(final_segments)
    artifact_paths["normalized_transcript_json"] = _write_json(
        store, meeting_id, NORMALIZED_TRANSCRIPT_JSON, normalized
    )
    artifact_paths["transcript_markdown"] = _write_text(store, meeting_id, TRANSCRIPT_MARKDOWN, markdown)
    store.add_transcript_segments(final_segments)
    _save_session(
        store,
        meeting_id=meeting_id,
        event=event,
        resource=resource,
        bot_payload=bot_payload,
        transcript_status="done" if final_segments else "failed",
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
    )

    if final_segments:
        artifact_paths[OUTBOX_TRANSCRIPT_KEY] = queue_transcript_notification(
            store,
            meeting_id=meeting_id,
            text=telegram_transcript,
            backend=delivery_backend,
            chat_id=delivery_chat_id,
            thread_id=delivery_thread_id,
        )
        store.update_session(meeting_id, telegram_delivery_status="queued")

    summary_result = ingest_recall_summary(
        store,
        meeting_id=meeting_id,
        resource=resource,
        bot_payload=bot_payload,
        directory=participant_directory,
        chat_id=delivery_chat_id,
        thread_id=delivery_thread_id,
        backend=delivery_backend,
        summary_recipient_emails=summary_recipient_emails,
    )
    if summary_result.available:
        artifact_paths.update(summary_result.artifact_paths)
        store.update_session(meeting_id, summary_status="done", telegram_delivery_status="queued")

    status = IngestStatus.COMPLETED if final_segments else IngestStatus.FAILED
    reason = "transcript_ingested" if status == IngestStatus.COMPLETED else "empty_transcript"
    logger.info(
        "Recall transcript ingest finished: meeting_id=%s provider=%s fallback=%s",
        meeting_id,
        provider,
        fallback_used,
    )
    return IngestResult(
        status=status,
        reason=reason,
        meeting_id=meeting_id,
        provider=provider,
        fallback_used=fallback_used,
        fallback_reason=fallback_reason,
        artifact_paths=artifact_paths,
        errors=errors,
    )


def ingest_completed_transcript(*args: Any, **kwargs: Any) -> IngestResult:
    """Alias kept for callers that prefer domain wording over webhook wording."""
    return process_recall_webhook(*args, **kwargs)


__all__ = [
    "IngestResult",
    "IngestStatus",
    "RAW_BOT_JSON",
    "RAW_TRANSCRIPT_RESOURCE_JSON",
    "RAW_TRANSCRIPT_JSON",
    "NORMALIZED_TRANSCRIPT_JSON",
    "TRANSCRIPT_MARKDOWN",
    "OUTBOX_LEFT_MEETING_KEY",
    "OUTBOX_TRANSCRIPT_KEY",
    "ingest_completed_transcript",
    "process_recall_webhook",
    "redact_status_value",
]
