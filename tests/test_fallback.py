"""Tests for the Recall.ai meeting-assistant OpenAI transcription fallback.

Covers Task 6 of docs/architecture.md:

* The fallback transcriber is injectable (a plain callable or an OpenAI
  client-like object), so no real network call is ever made.
* The fallback's only input is recording audio (bytes or a file path) — never
  the broken transcript text.
* Fallback output is normalized into the shared :class:`TranscriptSegment`
  format (provider/model recorded).
* When the fallback fails, a structured, non-secret failure result is returned
  and *no* fake transcript is produced.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from recall_meeting_assistant.models import TranscriptSegment
from recall_meeting_assistant.openai_fallback import (
    FallbackResult,
    normalize_openai_transcription,
    run_openai_fallback,
)

AUDIO = b"RIFF....fake-wav-bytes....data...."


# ── injectable transcriber doubles ───────────────────────────────────────────
class _RecordingCallable:
    """A callable transcriber double that records what it was given."""

    def __init__(self, result):
        self.result = result
        self.calls: list[dict] = []

    def __call__(self, *, audio, model, language=None):
        self.calls.append({"audio": audio, "model": model, "language": language})
        return self.result


def _verbose_response(text=" Hello there. This is the recap."):
    return {
        "text": text,
        "segments": [
            {"id": 0, "start": 0.0, "end": 2.0, "text": " Hello there."},
            {"id": 1, "start": 2.1, "end": 4.5, "text": " This is the recap.", "confidence": 0.9},
        ],
    }


# ── normalize_openai_transcription ───────────────────────────────────────────


def test_normalize_segments_from_verbose_dict():
    segments = normalize_openai_transcription(
        _verbose_response(), meeting_id="m_1", provider="openai", model="gpt-4o-transcribe"
    )
    assert len(segments) == 2
    assert all(isinstance(s, TranscriptSegment) for s in segments)
    assert segments[0].text == "Hello there."
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 2000
    assert segments[0].provider == "openai"
    assert segments[1].start_ms == 2100
    assert segments[1].confidence == pytest.approx(0.9)
    assert segments[0].segment_index == 0
    assert segments[1].segment_index == 1


def test_normalize_segments_from_object_with_attrs():
    resp = SimpleNamespace(
        text="Hello world.",
        segments=[SimpleNamespace(start=0.0, end=1.5, text="Hello world.")],
    )
    segments = normalize_openai_transcription(resp, meeting_id="m_2")
    assert len(segments) == 1
    assert segments[0].text == "Hello world."
    assert segments[0].end_ms == 1500


def test_normalize_plain_text_becomes_single_segment():
    segments = normalize_openai_transcription("Just the whole thing.", meeting_id="m_3")
    assert len(segments) == 1
    assert segments[0].text == "Just the whole thing."
    assert segments[0].start_ms == 0
    assert segments[0].end_ms == 0


def test_normalize_text_only_response_without_segments():
    segments = normalize_openai_transcription({"text": "No segments here."}, meeting_id="m_4")
    assert len(segments) == 1
    assert segments[0].text == "No segments here."


def test_normalize_empty_returns_no_segments():
    assert normalize_openai_transcription({"text": "   "}, meeting_id="m") == []
    assert normalize_openai_transcription("", meeting_id="m") == []
    assert normalize_openai_transcription(None, meeting_id="m") == []


# ── run_openai_fallback: success ─────────────────────────────────────────────


def test_fallback_success_with_callable():
    transcriber = _RecordingCallable(_verbose_response())
    result = run_openai_fallback(
        meeting_id="m_1",
        audio=AUDIO,
        transcriber=transcriber,
        model="gpt-4o-transcribe",
        trigger_reason="no_segments",
    )
    assert isinstance(result, FallbackResult)
    assert result.success is True
    assert result.fallback_used is True
    assert result.failure_reason is None
    assert result.provider == "openai"
    assert result.model == "gpt-4o-transcribe"
    assert result.trigger_reason == "no_segments"
    assert len(result.segments) == 2
    assert result.segments[0].provider == "openai"
    assert transcriber.calls[0]["language"] == "en"


def test_fallback_input_is_audio_bytes_not_transcript_text():
    transcriber = _RecordingCallable(_verbose_response())
    run_openai_fallback(
        meeting_id="m_1", audio=AUDIO, transcriber=transcriber, trigger_reason="provider_failure"
    )
    assert transcriber.calls, "transcriber was not invoked"
    sent = transcriber.calls[0]["audio"]
    assert sent == AUDIO
    assert isinstance(sent, (bytes, bytearray))


def test_fallback_reads_audio_from_path(tmp_path):
    audio_path = tmp_path / "recording.wav"
    audio_path.write_bytes(AUDIO)
    transcriber = _RecordingCallable(_verbose_response())
    result = run_openai_fallback(
        meeting_id="m_1", audio=audio_path, transcriber=transcriber, trigger_reason="download_failure"
    )
    assert result.success is True
    assert transcriber.calls[0]["audio"] == AUDIO


def test_fallback_with_openai_client_like_object():
    captured = {}

    def _create(*, model, file, response_format, **kwargs):
        captured["model"] = model
        captured["file"] = file
        captured["response_format"] = response_format
        return _verbose_response()

    client = SimpleNamespace(audio=SimpleNamespace(transcriptions=SimpleNamespace(create=_create)))
    result = run_openai_fallback(
        meeting_id="m_1",
        audio=AUDIO,
        transcriber=client,
        model="gpt-4o-mini-transcribe",
        trigger_reason="text_too_short",
    )
    assert result.success is True
    assert result.model == "gpt-4o-mini-transcribe"
    assert captured["model"] == "gpt-4o-mini-transcribe"
    # File payload carries the audio bytes (never text).
    assert AUDIO in tuple(captured["file"]) or captured["file"] == AUDIO


# ── run_openai_fallback: failure (no fake transcript) ────────────────────────


def test_fallback_transcriber_raises_returns_structured_failure():
    def _boom(*, audio, model, language=None):
        raise RuntimeError("openai upstream 500")

    result = run_openai_fallback(
        meeting_id="m_1", audio=AUDIO, transcriber=_boom, trigger_reason="no_segments"
    )
    assert result.success is False
    assert result.segments == []
    assert result.fallback_used is True
    assert result.failure_reason is not None
    assert result.trigger_reason == "no_segments"


def test_fallback_failure_reason_is_non_secret():
    api_value = "example-provider-value"

    def _boom(*, audio, model, language=None):
        raise RuntimeError(f"auth failed for key {api_value}")

    result = run_openai_fallback(
        meeting_id="m_1", audio=AUDIO, transcriber=_boom, trigger_reason="provider_failure"
    )
    assert result.success is False
    assert api_value not in (result.failure_reason or "")
    assert api_value not in repr(result)


def test_fallback_empty_audio_short_circuits_without_calling_transcriber():
    transcriber = _RecordingCallable(_verbose_response())
    result = run_openai_fallback(
        meeting_id="m_1", audio=b"", transcriber=transcriber, trigger_reason="download_failure"
    )
    assert result.success is False
    assert result.failure_reason == "empty_audio"
    assert transcriber.calls == []


def test_fallback_empty_transcript_is_failure_not_fake():
    transcriber = _RecordingCallable({"text": "   ", "segments": []})
    result = run_openai_fallback(
        meeting_id="m_1", audio=AUDIO, transcriber=transcriber, trigger_reason="no_segments"
    )
    assert result.success is False
    assert result.failure_reason == "empty_transcript"
    assert result.segments == []


def test_package_exports_fallback_and_transcript_symbols():
    import recall_meeting_assistant as pkg

    assert hasattr(pkg, "run_openai_fallback")
    assert hasattr(pkg, "normalize_transcript")
    assert hasattr(pkg, "assess_transcript_quality")
