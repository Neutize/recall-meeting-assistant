"""Transcript normalization + quality gates for the meeting-assistant package.

Task 5 of docs/architecture.md.  Recall returns
transcripts in a few different shapes depending on provider/config; this module
collapses the common ones into the shared :class:`TranscriptSegment` model,
then renders JSON-ready and Markdown views and runs the quality gates that
decide whether the OpenAI fallback should fire.

Supported raw shapes (auto-detected):

* **Modern diarized blocks** — a list of ``{"participant": {...}, "words": [...]}``
  where each word carries ``start_timestamp``/``end_timestamp`` (either a bare
  number of seconds or ``{"relative": <seconds>}``).
* **Legacy diarized blocks** — ``{"speaker": ..., "words": [...]}`` with numeric
  word timestamps and optional per-word ``confidence``.
* **Flat segment lists** — ``[{"speaker", "start", "end", "text", "confidence"}]``,
  optionally wrapped in a ``{"transcript"|"segments"|"results": [...]}`` envelope.

This module performs no I/O and logs nothing transcript-derived, so it is safe
to run on the request path or in a worker.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from recall_meeting_assistant.models import TranscriptSegment

DEFAULT_PROVIDER = "recallai_streaming"

# ── Quality-gate tuning + reason codes ───────────────────────────────────────
#: Minimum total transcript text (chars) expected from a non-trivial meeting.
DEFAULT_MIN_TEXT_CHARS = 200
#: A meeting at/over this many minutes is "non-trivial" for the length check.
DEFAULT_NONTRIVIAL_MINUTES = 2.0

REASON_NO_SEGMENTS = "no_segments"
REASON_TEXT_TOO_SHORT = "text_too_short"
REASON_MISSING_SPEAKERS = "missing_speakers"
REASON_PROVIDER_FAILURE = "provider_failure"
REASON_DOWNLOAD_FAILURE = "download_failure"

# Envelope keys that wrap the actual list of transcript entries.
_ENVELOPE_KEYS = ("transcript", "segments", "results", "monologues", "utterances")


# ── timestamp / field helpers ────────────────────────────────────────────────
def _seconds_to_ms(value: Any) -> int | None:
    """Coerce a Recall timestamp into integer milliseconds.

    Accepts a bare number of seconds or a ``{"relative": <seconds>}`` /
    ``{"absolute": ...}`` object.  ``None``/unparseable → ``None``.
    """
    if value is None:
        return None
    if isinstance(value, dict):
        for key in ("relative", "absolute", "seconds"):
            if key in value:
                return _seconds_to_ms(value[key])
        return None
    try:
        return int(round(float(value) * 1000))
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    for value in values:
        if value is not None and not (isinstance(value, str) and not value.strip()):
            return value
    return None


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _speaker_of(entry: dict[str, Any]) -> str | None:
    participant = entry.get("participant") if isinstance(entry.get("participant"), dict) else None
    return _clean_str(
        _first(
            entry.get("speaker"),
            entry.get("speaker_name"),
            entry.get("speaker_label"),
            participant.get("name") if participant else None,
        )
    )


def _mean_confidence(values: list[float]) -> float | None:
    real = [v for v in values if isinstance(v, (int, float))]
    if not real:
        return None
    return sum(real) / len(real)


# ── shape detection + entry conversion ───────────────────────────────────────
def _unwrap(raw: Any) -> list[Any]:
    """Return the list of transcript entries from any supported envelope."""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in _ENVELOPE_KEYS:
            value = raw.get(key)
            if isinstance(value, list):
                return value
        # A single flat segment dict.
        if any(k in raw for k in ("text", "words", "transcript")):
            return [raw]
    return []


def _segment_from_words(entry: dict[str, Any]) -> tuple[str, int | None, int | None, float | None]:
    """Collapse a diarized ``words`` block into (text, start_ms, end_ms, conf)."""
    words = entry.get("words") or []
    texts: list[str] = []
    confidences: list[float] = []
    start_ms: int | None = None
    end_ms: int | None = None
    for word in words:
        if not isinstance(word, dict):
            continue
        token = _clean_str(_first(word.get("text"), word.get("word")))
        if token:
            texts.append(token)
        ws = _seconds_to_ms(_first(word.get("start_timestamp"), word.get("start"), word.get("start_time")))
        we = _seconds_to_ms(_first(word.get("end_timestamp"), word.get("end"), word.get("end_time")))
        if ws is not None:
            start_ms = ws if start_ms is None else min(start_ms, ws)
        if we is not None:
            end_ms = we if end_ms is None else max(end_ms, we)
        conf = word.get("confidence")
        if isinstance(conf, (int, float)):
            confidences.append(conf)
    return " ".join(texts), start_ms, end_ms, _mean_confidence(confidences)


def _segment_from_flat(entry: dict[str, Any]) -> tuple[str, int | None, int | None, float | None]:
    text = _clean_str(_first(entry.get("text"), entry.get("transcript"), entry.get("value"))) or ""
    start_ms = _seconds_to_ms(
        _first(entry.get("start"), entry.get("start_time"), entry.get("start_timestamp"), entry.get("offset"))
    )
    end_ms = _seconds_to_ms(
        _first(entry.get("end"), entry.get("end_time"), entry.get("end_timestamp"))
    )
    conf = entry.get("confidence")
    confidence = float(conf) if isinstance(conf, (int, float)) else None
    return text, start_ms, end_ms, confidence


def normalize_transcript(
    raw: Any, *, meeting_id: str, provider: str = DEFAULT_PROVIDER
) -> list[TranscriptSegment]:
    """Normalize a raw Recall transcript into ordered :class:`TranscriptSegment`s.

    Speaker, timestamps (ms), text, provider, and confidence are preserved when
    present.  Blank-text entries are dropped.  Segment indices are assigned
    monotonically in source order.
    """
    entries = _unwrap(raw)
    segments: list[TranscriptSegment] = []
    index = 0
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        if isinstance(entry.get("words"), list):
            text, start_ms, end_ms, confidence = _segment_from_words(entry)
        else:
            text, start_ms, end_ms, confidence = _segment_from_flat(entry)
        if not text.strip():
            continue
        segments.append(
            TranscriptSegment(
                meeting_id=meeting_id,
                segment_index=index,
                start_ms=start_ms or 0,
                end_ms=end_ms or (start_ms or 0),
                text=text,
                speaker_raw=_speaker_of(entry),
                provider=provider,
                confidence=confidence,
                raw_json=entry,
            )
        )
        index += 1
    return segments


# ── JSON-ready + Markdown rendering ──────────────────────────────────────────
def _speaker_order(segments: list[TranscriptSegment]) -> list[str]:
    seen: dict[str, None] = {}
    for seg in segments:
        if seg.speaker_raw:
            seen.setdefault(seg.speaker_raw, None)
    return sorted(seen)


def transcript_to_json(
    segments: list[TranscriptSegment],
    *,
    meeting_id: str,
    provider: str = DEFAULT_PROVIDER,
    title: str | None = None,
) -> dict[str, Any]:
    """Build a JSON-ready normalized transcript document."""
    return {
        "meeting_id": meeting_id,
        "provider": provider,
        "title": title,
        "segment_count": len(segments),
        "speakers": _speaker_order(segments),
        "total_text_chars": sum(len(s.text) for s in segments),
        "segments": [s.to_dict() for s in segments],
    }


def _format_timestamp(ms: int) -> str:
    total_seconds = max(0, ms) // 1000
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def transcript_to_markdown(
    segments: list[TranscriptSegment], *, title: str | None = None
) -> str:
    """Render a readable Markdown transcript (one line per segment)."""
    heading = title or "Meeting Transcript"
    lines = [f"# {heading}", ""]
    if not segments:
        lines.append("_No transcript segments available._")
        return "\n".join(lines)
    for seg in segments:
        speaker = seg.speaker_raw or "Unknown"
        stamp = _format_timestamp(seg.start_ms)
        lines.append(f"**[{stamp}] {speaker}:** {seg.text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def transcript_to_telegram(
    segments: list[TranscriptSegment], *, title: str | None = None
) -> str:
    """Render the complete transcript using the small Telegram Markdown subset."""

    heading = title or "Meeting transcript"
    lines = [f"**{heading}**", ""]
    if not segments:
        lines.append("No transcript segments available.")
    else:
        for seg in segments:
            speaker = seg.speaker_raw or "Unknown"
            stamp = _format_timestamp(seg.start_ms)
            lines.append(f"**[{stamp}] {speaker}:** {seg.text}")
    return "\n".join(lines).rstrip()


# ── quality gates ────────────────────────────────────────────────────────────
@dataclass
class TranscriptQualityResult:
    """Verdict on whether a normalized transcript is usable.

    ``reasons`` holds stable ``REASON_*`` codes (empty when usable).
    ``should_fallback`` is the inverse of ``usable`` — when any gate trips, the
    pipeline should run the OpenAI fallback rather than ship a broken transcript.
    """

    usable: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def should_fallback(self) -> bool:
        return not self.usable


def assess_transcript_quality(
    segments: list[TranscriptSegment],
    *,
    participant_count: int | None = None,
    meeting_duration_minutes: float | None = None,
    provider_failed: bool = False,
    download_failed: bool = False,
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    nontrivial_minutes: float = DEFAULT_NONTRIVIAL_MINUTES,
) -> TranscriptQualityResult:
    """Flag empty / obviously broken transcripts that should trigger fallback.

    A transcript is unusable when any of these hold:

    * the recording/transcript download failed (``download_failed``),
    * the transcript provider reported failure (``provider_failed``),
    * there are no segments,
    * total text is below ``min_text_chars`` for a non-trivial meeting
      (duration ``>= nontrivial_minutes``, or unknown duration), or
    * every speaker label is missing while more than one participant attended.
    """
    reasons: list[str] = []

    if download_failed:
        reasons.append(REASON_DOWNLOAD_FAILURE)
    if provider_failed:
        reasons.append(REASON_PROVIDER_FAILURE)

    if not segments:
        reasons.append(REASON_NO_SEGMENTS)
        return TranscriptQualityResult(usable=False, reasons=reasons)

    total_chars = sum(len(s.text) for s in segments)
    is_nontrivial = meeting_duration_minutes is None or meeting_duration_minutes >= nontrivial_minutes
    if is_nontrivial and total_chars < min_text_chars:
        reasons.append(REASON_TEXT_TOO_SHORT)

    has_any_speaker = any(s.speaker_raw for s in segments)
    if not has_any_speaker and (participant_count or 0) > 1:
        reasons.append(REASON_MISSING_SPEAKERS)

    return TranscriptQualityResult(usable=not reasons, reasons=reasons)


__all__ = [
    "DEFAULT_PROVIDER",
    "DEFAULT_MIN_TEXT_CHARS",
    "DEFAULT_NONTRIVIAL_MINUTES",
    "REASON_NO_SEGMENTS",
    "REASON_TEXT_TOO_SHORT",
    "REASON_MISSING_SPEAKERS",
    "REASON_PROVIDER_FAILURE",
    "REASON_DOWNLOAD_FAILURE",
    "TranscriptQualityResult",
    "normalize_transcript",
    "transcript_to_json",
    "transcript_to_markdown",
    "transcript_to_telegram",
    "assess_transcript_quality",
]
