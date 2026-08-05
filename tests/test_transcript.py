"""Tests for the Recall.ai meeting-assistant transcript pipeline.

Covers Task 5 of docs/architecture.md:

* Common Recall transcript shapes (modern ``participant``/``words`` blocks,
  legacy ``speaker``/``words`` blocks, and flat segment lists, optionally
  wrapped in a ``transcript``/``segments`` envelope) normalise into the shared
  :class:`TranscriptSegment` model, preserving speaker, timestamp, text,
  provider, and confidence when available.
* A JSON-ready normalized structure and a readable Markdown transcript are
  produced.
* Quality gates flag empty/unusable transcripts: no segments, too-short text
  for a non-trivial meeting, missing speakers in a multi-speaker meeting,
  provider failure, and download failure.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recall_meeting_assistant.models import TranscriptSegment
from recall_meeting_assistant.transcript import (
    DEFAULT_MIN_TEXT_CHARS,
    DEFAULT_NONTRIVIAL_MINUTES,
    REASON_DOWNLOAD_FAILURE,
    REASON_MISSING_SPEAKERS,
    REASON_NO_SEGMENTS,
    REASON_PROVIDER_FAILURE,
    REASON_TEXT_TOO_SHORT,
    TranscriptQualityResult,
    assess_transcript_quality,
    normalize_transcript,
    transcript_to_json,
    transcript_to_markdown,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
RECALL = FIXTURES / "recall"
MEETINGS = FIXTURES / "meetings"


def _load(path: Path):
    return json.loads(path.read_text())


# ── normalization: modern participant/words shape ────────────────────────────


def test_normalize_modern_participant_words():
    raw = _load(RECALL / "transcript_download.json")
    segments = normalize_transcript(raw, meeting_id="m_1", provider="recallai_streaming")

    assert len(segments) == 3
    assert all(isinstance(s, TranscriptSegment) for s in segments)

    first = segments[0]
    assert first.meeting_id == "m_1"
    assert first.segment_index == 0
    assert first.speaker_raw == "Speaker A"
    assert first.provider == "recallai_streaming"
    assert first.text == "Hi everyone, thanks for joining the project sync."
    # Relative seconds → milliseconds.
    assert first.start_ms == 500
    assert first.end_ms == 4500

    # Second block is a different speaker.
    assert segments[1].speaker_raw == "Speaker B"
    assert segments[2].segment_index == 2


def test_normalize_preserves_monotonic_segment_indices():
    raw = _load(RECALL / "transcript_download.json")
    segments = normalize_transcript(raw, meeting_id="m_1")
    assert [s.segment_index for s in segments] == [0, 1, 2]


# ── normalization: legacy speaker/words shape with confidence ────────────────


def test_normalize_legacy_words_with_confidence():
    raw = _load(RECALL / "transcript_legacy_words.json")
    segments = normalize_transcript(raw, meeting_id="m_2")

    assert len(segments) == 2
    assert segments[0].speaker_raw == "Speaker A"
    assert segments[0].text == "Quick status update."
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 1600
    # Confidence is the mean of the per-word confidences, preserved.
    assert segments[0].confidence == pytest.approx((0.98 + 0.97 + 0.95) / 3)
    assert segments[1].speaker_raw == "Speaker B"


# ── normalization: flat segment list (wrapped) ───────────────────────────────


def test_normalize_flat_segments_envelope():
    raw = _load(RECALL / "transcript_flat_segments.json")
    segments = normalize_transcript(raw, meeting_id="m_3")

    assert len(segments) == 2
    assert segments[0].speaker_raw == "Speaker A"
    assert segments[0].text == "Let's lock the agenda."
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 2000
    assert segments[0].confidence == pytest.approx(0.94)
    assert segments[1].start_ms == 2500


def test_normalize_empty_returns_no_segments():
    assert normalize_transcript([], meeting_id="m_4") == []
    assert normalize_transcript({"transcript": []}, meeting_id="m_4") == []
    assert normalize_transcript(None, meeting_id="m_4") == []


def test_normalize_skips_blank_text_entries():
    raw = {"transcript": [{"speaker": "A", "start": 0, "end": 1, "text": "  "}]}
    assert normalize_transcript(raw, meeting_id="m_5") == []


# ── JSON-ready structure ─────────────────────────────────────────────────────


def test_transcript_to_json_is_serializable_and_complete():
    raw = _load(RECALL / "transcript_download.json")
    segments = normalize_transcript(raw, meeting_id="m_1", provider="recallai_streaming")
    doc = transcript_to_json(segments, meeting_id="m_1", provider="recallai_streaming", title="Example project sync")

    # Must be JSON round-trippable.
    reloaded = json.loads(json.dumps(doc))
    assert reloaded["meeting_id"] == "m_1"
    assert reloaded["provider"] == "recallai_streaming"
    assert reloaded["title"] == "Example project sync"
    assert reloaded["segment_count"] == 3
    assert reloaded["speakers"] == ["Speaker A", "Speaker B"]
    assert reloaded["total_text_chars"] > 0
    assert len(reloaded["segments"]) == 3
    assert reloaded["segments"][0]["speaker_raw"] == "Speaker A"


# ── Markdown rendering ───────────────────────────────────────────────────────


def test_transcript_to_markdown_has_title_speakers_and_timestamps():
    raw = _load(RECALL / "transcript_download.json")
    segments = normalize_transcript(raw, meeting_id="m_1")
    md = transcript_to_markdown(segments, title="Example project sync")

    assert md.startswith("# Example project sync")
    assert "Speaker A" in md
    assert "Speaker B" in md
    # Timestamp HH:MM:SS for the first segment (0.5s → 00:00:00).
    assert "00:00:00" in md
    assert "Hi everyone, thanks for joining the project sync." in md


def test_transcript_to_markdown_unknown_speaker_label():
    raw = _load(RECALL / "transcript_no_speakers.json")
    segments = normalize_transcript(raw, meeting_id="m_6")
    md = transcript_to_markdown(segments)
    assert "Unknown" in md


# ── quality gates ────────────────────────────────────────────────────────────


def test_quality_good_transcript_is_usable():
    raw = _load(MEETINGS / "sample_transcript.json")
    segments = normalize_transcript(raw, meeting_id="m_z")
    result = assess_transcript_quality(
        segments, participant_count=2, meeting_duration_minutes=30
    )
    assert isinstance(result, TranscriptQualityResult)
    assert result.usable is True
    assert result.reasons == []
    assert result.should_fallback is False


def test_quality_no_segments_flags_and_triggers_fallback():
    result = assess_transcript_quality([], participant_count=3, meeting_duration_minutes=30)
    assert result.usable is False
    assert REASON_NO_SEGMENTS in result.reasons
    assert result.should_fallback is True


def test_quality_text_too_short_for_long_meeting():
    segments = [
        TranscriptSegment(meeting_id="m", segment_index=0, start_ms=0, end_ms=1000, text="ok", speaker_raw="A")
    ]
    result = assess_transcript_quality(
        segments, participant_count=2, meeting_duration_minutes=45
    )
    assert result.usable is False
    assert REASON_TEXT_TOO_SHORT in result.reasons


def test_quality_short_meeting_does_not_flag_short_text():
    segments = [
        TranscriptSegment(meeting_id="m", segment_index=0, start_ms=0, end_ms=1000, text="ok", speaker_raw="A")
    ]
    result = assess_transcript_quality(
        segments, participant_count=1, meeting_duration_minutes=0.5
    )
    assert REASON_TEXT_TOO_SHORT not in result.reasons


def test_quality_missing_speakers_multi_participant():
    raw = _load(RECALL / "transcript_no_speakers.json")
    segments = normalize_transcript(raw, meeting_id="m_6")
    result = assess_transcript_quality(
        segments, participant_count=3, meeting_duration_minutes=1
    )
    assert REASON_MISSING_SPEAKERS in result.reasons
    assert result.usable is False


def test_quality_missing_speakers_ignored_for_single_participant():
    raw = _load(RECALL / "transcript_no_speakers.json")
    segments = normalize_transcript(raw, meeting_id="m_6")
    result = assess_transcript_quality(
        segments, participant_count=1, meeting_duration_minutes=1
    )
    assert REASON_MISSING_SPEAKERS not in result.reasons


def test_quality_provider_failure_flag():
    raw = _load(MEETINGS / "sample_transcript.json")
    segments = normalize_transcript(raw, meeting_id="m_z")
    result = assess_transcript_quality(segments, provider_failed=True)
    assert REASON_PROVIDER_FAILURE in result.reasons
    assert result.should_fallback is True


def test_quality_download_failure_flag():
    result = assess_transcript_quality([], download_failed=True)
    assert REASON_DOWNLOAD_FAILURE in result.reasons
    assert result.should_fallback is True


def test_quality_defaults_exposed():
    assert DEFAULT_MIN_TEXT_CHARS > 0
    assert DEFAULT_NONTRIVIAL_MINUTES > 0
