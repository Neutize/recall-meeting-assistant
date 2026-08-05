"""Standalone Recall.ai meeting assistant.

The package creates Recall.ai bots, verifies webhooks, normalizes transcripts,
persists private artifacts, and queues delivery messages for a configured
adapter such as Telegram.
"""

from __future__ import annotations

from recall_meeting_assistant.config import RecallConfigError, RecallMeetingsConfig, load_config
from recall_meeting_assistant.ingest import (
    IngestResult,
    IngestStatus,
    ingest_completed_transcript,
    process_recall_webhook,
    redact_status_value,
)
from recall_meeting_assistant.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingSession,
    TranscriptSegment,
    redact_meeting_url,
)
from recall_meeting_assistant.openai_fallback import (
    FallbackResult,
    normalize_openai_transcription,
    run_openai_fallback,
)
from recall_meeting_assistant.outbox import (
    DEFAULT_LEFT_MEETING_TEXT,
    OutboxMessage,
    queue_left_meeting_notification,
    queue_summary_notification,
    queue_transcript_notification,
    write_outbox_message,
)
from recall_meeting_assistant.transcript import (
    TranscriptQualityResult,
    assess_transcript_quality,
    normalize_transcript,
    transcript_to_json,
    transcript_to_markdown,
    transcript_to_telegram,
)
from recall_meeting_assistant.webhooks import (
    QueuedWebhookResult,
    WebhookEvent,
    WebhookVerificationError,
    handle_webhook,
    parse_event,
    sign_body,
    verify_signature,
    verify_url_token,
)

__all__ = [
    "RecallConfigError",
    "RecallMeetingsConfig",
    "load_config",
    "IngestResult",
    "IngestStatus",
    "ingest_completed_transcript",
    "process_recall_webhook",
    "redact_status_value",
    "MeetingSession",
    "MeetingParticipant",
    "TranscriptSegment",
    "MeetingActionItem",
    "redact_meeting_url",
    "QueuedWebhookResult",
    "WebhookEvent",
    "WebhookVerificationError",
    "handle_webhook",
    "parse_event",
    "sign_body",
    "verify_signature",
    "verify_url_token",
    "TranscriptQualityResult",
    "assess_transcript_quality",
    "normalize_transcript",
    "transcript_to_json",
    "transcript_to_markdown",
    "transcript_to_telegram",
    "FallbackResult",
    "normalize_openai_transcription",
    "run_openai_fallback",
    "DEFAULT_LEFT_MEETING_TEXT",
    "OutboxMessage",
    "queue_left_meeting_notification",
    "queue_transcript_notification",
    "queue_summary_notification",
    "write_outbox_message",
]
