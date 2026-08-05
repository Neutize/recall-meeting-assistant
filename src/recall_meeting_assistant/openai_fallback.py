"""OpenAI transcription fallback for the meeting-assistant package.

Task 6 of docs/architecture.md.  When the
primary Recall transcript is missing or the quality gates (see
:mod:`recall_meeting_assistant.transcript`) flag it as unusable, this module
re-transcribes the *recording audio* with OpenAI and normalizes the result into
the shared :class:`TranscriptSegment` format.

Design invariants:

1. **Audio in, never broken transcript text.**  The only acceptable input is
   recording audio (raw bytes or a file path).  The fallback re-runs speech-to-
   text from the source; it never tries to "repair" a garbled transcript.
2. **Injectable transcriber.**  The transcriber is passed in — either a plain
   callable ``transcriber(audio=..., model=..., language=...)`` or an OpenAI
   client-like object exposing ``.audio.transcriptions.create(...)``.  Tests
   inject doubles, so no real network call is ever made.
3. **No fake transcript on failure.**  Any error (or an empty result) yields a
   structured :class:`FallbackResult` with ``success=False`` and a short,
   *non-secret* ``failure_reason`` — never fabricated segments.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from recall_meeting_assistant.config import DEFAULT_OPENAI_FALLBACK_MODEL
from recall_meeting_assistant.models import TranscriptSegment

logger = logging.getLogger(__name__)

DEFAULT_PROVIDER = "openai"
DEFAULT_LANGUAGE = "en"
#: Filename hint passed to the OpenAI client file tuple (content, not name,
#: matters; kept generic so no meeting identifier leaks into the upload).
_AUDIO_FILENAME = "recording.wav"
#: Cap on the redacted failure-reason string surfaced in results/logs.
_MAX_REASON_CHARS = 200


@dataclass
class FallbackResult:
    """Outcome of an OpenAI fallback transcription attempt.

    On success, ``segments`` holds normalized :class:`TranscriptSegment`s and
    ``failure_reason`` is ``None``.  On failure, ``segments`` is empty and
    ``failure_reason`` is a short non-secret code/message.  ``trigger_reason``
    records *why* the fallback was invoked (the quality-gate code).
    """

    success: bool
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_OPENAI_FALLBACK_MODEL
    trigger_reason: str | None = None
    failure_reason: str | None = None
    fallback_used: bool = True
    segments: list[TranscriptSegment] = field(default_factory=list)


# ── helpers ──────────────────────────────────────────────────────────────────
def _attr_or_key(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _seconds_to_ms(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _read_audio(audio: bytes | bytearray | str | Path) -> bytes:
    """Return raw audio bytes from bytes or a filesystem path.

    Raises :class:`FileNotFoundError`/``OSError`` for an unreadable path; the
    caller turns that into a structured failure result.
    """
    if isinstance(audio, (bytes, bytearray)):
        return bytes(audio)
    return Path(audio).read_bytes()


def _safe_reason(message: str) -> str:
    """Redact and truncate an error message so no secret reaches logs/results."""
    _ = message
    # Provider exceptions can contain arbitrary request data, credentials, or
    # signed URLs. Do not echo the provider message even after pattern-based
    # redaction; callers only need a stable retryable reason.
    return "transcriber_error"


def normalize_openai_transcription(
    raw: Any,
    *,
    meeting_id: str,
    provider: str = DEFAULT_PROVIDER,
    model: str | None = None,
) -> list[TranscriptSegment]:
    """Normalize an OpenAI transcription response into transcript segments.

    Accepts a verbose response (dict or object exposing ``segments`` and
    ``text``) or a plain transcript string.  OpenAI does not diarize, so
    ``speaker_raw`` is left ``None``.  Empty/whitespace input yields no
    segments (so callers never ship a fake transcript).
    """
    if raw is None:
        return []

    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return []
        return [
            TranscriptSegment(
                meeting_id=meeting_id,
                segment_index=0,
                start_ms=0,
                end_ms=0,
                text=text,
                speaker_raw=None,
                provider=provider,
                confidence=None,
                raw_json={"text": text, "model": model} if model else {"text": text},
            )
        ]

    raw_segments = _attr_or_key(raw, "segments")
    segments: list[TranscriptSegment] = []
    if isinstance(raw_segments, list) and raw_segments:
        index = 0
        for seg in raw_segments:
            text = str(_attr_or_key(seg, "text") or "").strip()
            if not text:
                continue
            conf = _attr_or_key(seg, "confidence")
            segments.append(
                TranscriptSegment(
                    meeting_id=meeting_id,
                    segment_index=index,
                    start_ms=_seconds_to_ms(_attr_or_key(seg, "start")) or 0,
                    end_ms=_seconds_to_ms(_attr_or_key(seg, "end")) or 0,
                    text=text,
                    speaker_raw=None,
                    provider=provider,
                    confidence=float(conf) if isinstance(conf, (int, float)) else None,
                    raw_json=seg if isinstance(seg, dict) else None,
                )
            )
            index += 1
        if segments:
            return segments

    # No usable segment list — fall back to the flat ``text`` field.
    text = str(_attr_or_key(raw, "text") or "").strip()
    if not text:
        return []
    return [
        TranscriptSegment(
            meeting_id=meeting_id,
            segment_index=0,
            start_ms=0,
            end_ms=0,
            text=text,
            speaker_raw=None,
            provider=provider,
            confidence=None,
            raw_json={"text": text},
        )
    ]


def _invoke_transcriber(
    transcriber: Any, *, audio_bytes: bytes, model: str, language: str
) -> Any:
    """Call the injected transcriber, supporting both supported shapes."""
    create = getattr(getattr(transcriber, "audio", None), "transcriptions", None)
    create = getattr(create, "create", None)
    if create is not None:
        return create(
            model=model,
            file=(_AUDIO_FILENAME, audio_bytes),
            response_format="verbose_json",
            language=language,
        )
    if callable(transcriber):
        return transcriber(audio=audio_bytes, model=model, language=language)
    raise TypeError("transcriber must be callable or an OpenAI client-like object.")


def run_openai_fallback(
    *,
    meeting_id: str,
    audio: bytes | bytearray | str | Path,
    transcriber: Callable[..., Any] | Any,
    model: str = DEFAULT_OPENAI_FALLBACK_MODEL,
    trigger_reason: str | None = None,
    provider: str = DEFAULT_PROVIDER,
    language: str = DEFAULT_LANGUAGE,
) -> FallbackResult:
    """Re-transcribe recording ``audio`` with OpenAI as a transcript fallback.

    ``audio`` is raw bytes or a path to the recording; ``transcriber`` is the
    injected client/callable.  Returns a :class:`FallbackResult`: on success it
    carries normalized segments; on any error or empty result it carries
    ``success=False`` with a non-secret ``failure_reason`` and no segments.
    """
    def _failure(reason: str) -> FallbackResult:
        return FallbackResult(
            success=False,
            provider=provider,
            model=model,
            trigger_reason=trigger_reason,
            failure_reason=reason,
        )

    try:
        audio_bytes = _read_audio(audio)
    except OSError:
        # Path missing/unreadable — do not echo the path (may be sensitive).
        logger.warning("OpenAI fallback: recording audio could not be read")
        return _failure("audio_unavailable")

    if not audio_bytes:
        logger.warning("OpenAI fallback: empty recording audio")
        return _failure("empty_audio")

    try:
        raw = _invoke_transcriber(
            transcriber, audio_bytes=audio_bytes, model=model, language=language
        )
    except Exception as exc:  # noqa: BLE001 — surface as structured failure
        logger.warning("OpenAI fallback transcription failed")
        return _failure(_safe_reason(str(exc)))

    segments = normalize_openai_transcription(
        raw, meeting_id=meeting_id, provider=provider, model=model
    )
    if not segments:
        logger.warning("OpenAI fallback produced no usable segments")
        return _failure("empty_transcript")

    logger.info("OpenAI fallback transcription produced %d segments", len(segments))
    return FallbackResult(
        success=True,
        provider=provider,
        model=model,
        trigger_reason=trigger_reason,
        failure_reason=None,
        segments=segments,
    )


__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_LANGUAGE",
    "FallbackResult",
    "normalize_openai_transcription",
    "run_openai_fallback",
]
