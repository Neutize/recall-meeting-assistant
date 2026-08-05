"""Tests for the Recall-native summary notes source + summary outbox payloads.

The MVP uses Recall's *native* meeting summary/analysis artifact as the default
notes source (rather than a custom summarizer).  These tests cover tolerant
extraction, safe behavior when no native summary is available, the rendered
Telegram body, and the queued ``meeting_summary`` outbox payload.
"""

from __future__ import annotations

import json
from pathlib import Path

from recall_meeting_assistant.outbox import SUMMARY_OUTBOX_FILENAME
from recall_meeting_assistant.participants import ParticipantDirectory
from recall_meeting_assistant.recall_summary import (
    RECALL_SUMMARY_JSON,
    RECALL_SUMMARY_PROVIDER,
    SUMMARY_MARKDOWN,
    RecallNativeSummary,
    build_summary_message,
    extract_recall_summary,
    ingest_recall_summary,
)
from recall_meeting_assistant.storage import MeetingStore

RESOURCE_WITH_SUMMARY = {
    "id": "tr_1",
    "summary": {
        "title": "Example project sync",
        "executive_summary": [
            "Reviewed the partner pipeline.",
            "Agreed Person Two sends the recap.",
        ],
        "action_items": [
            {"task": "Send the recap", "owner": "Person Two", "due_date": "Friday"},
            {"task": "Follow up partner X", "owner": "Alex"},
        ],
        "decisions": [{"text": "Prioritize partner X"}],
        "open_questions": [{"question": "What is the budget?"}],
    },
}

PEOPLE = {
    "people": [
        {"canonical_name": "Person Two", "meet_aliases": ["Person Two", "Second Person"], "telegram": {"user_id": "123456"}},
        {"canonical_name": "Alex Public", "meet_aliases": ["Alex"]},
        {"canonical_name": "Alex Quinn", "meet_aliases": ["Alex"]},
    ]
}


# ── extraction ───────────────────────────────────────────────────────────────
def test_extract_finds_native_summary_and_normalizes_fields():
    summary = extract_recall_summary(RESOURCE_WITH_SUMMARY)
    assert isinstance(summary, RecallNativeSummary)
    assert summary.title == "Example project sync"
    assert summary.executive_summary == [
        "Reviewed the partner pipeline.",
        "Agreed Person Two sends the recap.",
    ]
    assert summary.action_items[0] == {
        "task": "Send the recap",
        "owner": "Person Two",
        "due_date": "Friday",
    }
    assert summary.decisions[0]["text"] == "Prioritize partner X"
    assert summary.open_questions[0]["question"] == "What is the budget?"
    assert summary.is_empty is False


def test_extract_handles_string_summary_and_alternate_keys():
    summary = extract_recall_summary(
        {"analysis": {"short_summary": "Quick chat.", "action_points": ["Ping Person Two"]}}
    )
    assert summary is not None
    assert summary.short_summary == "Quick chat."
    # A bare string action point is coerced into a task dict.
    assert summary.action_items[0]["task"] == "Ping Person Two"


def test_extract_searches_nested_data_envelope():
    summary = extract_recall_summary({"data": {"ai_summary": {"key_points": ["A", "B"]}}})
    assert summary is not None
    assert summary.executive_summary == ["A", "B"]


def test_extract_returns_none_when_no_summary_present():
    assert extract_recall_summary({"id": "tr_1", "transcript": []}) is None
    assert extract_recall_summary({}) is None


def test_extract_returns_none_for_empty_summary_container():
    assert extract_recall_summary({"summary": {"executive_summary": [], "action_items": []}}) is None


def test_extract_searches_extra_sources():
    summary = extract_recall_summary({"id": "tr"}, {"summary": {"key_points": ["from bot"]}})
    assert summary is not None
    assert summary.executive_summary == ["from bot"]


# ── rendering ────────────────────────────────────────────────────────────────
def test_build_summary_message_tags_confident_owner_and_leaves_ambiguous_plain():
    directory = ParticipantDirectory.from_mapping(PEOPLE)
    summary = extract_recall_summary(RESOURCE_WITH_SUMMARY)
    notes = summary.to_meeting_notes("m_1")
    text = build_summary_message(notes, title=summary.title, directory=directory)

    assert "Example project sync" in text
    assert "Reviewed the partner pipeline." in text
    # Person Two is a confident, taggable match.
    assert "[Person Two](tg://user?id=123456)" in text
    # "Alex" is ambiguous -> shown as plain text, never tagged.
    assert "Alex" in text
    assert "@Alex" not in text


def test_build_summary_message_without_directory_uses_plain_owners():
    summary = extract_recall_summary(RESOURCE_WITH_SUMMARY)
    notes = summary.to_meeting_notes("m_1")
    text = build_summary_message(notes, title=summary.title)
    assert "Person Two" in text


# ── ingest integration ───────────────────────────────────────────────────────
def test_ingest_recall_summary_persists_artifacts_notes_and_queues_outbox(tmp_path: Path):
    store = MeetingStore(tmp_path)
    meeting_id = "recall_bot_1"
    # A transcript.md already exists from transcript ingest; it should be attached.
    transcript_path = store.artifact_path(meeting_id, "transcript.md", create_parents=True)
    transcript_path.write_text("# Transcript\n")

    result = ingest_recall_summary(
        store,
        meeting_id=meeting_id,
        resource=RESOURCE_WITH_SUMMARY,
        directory=ParticipantDirectory.from_mapping(PEOPLE),
        chat_id="-100123",
        thread_id="42",
    )

    assert result.available is True
    assert set(result.artifact_paths) == {
        "recall_summary_json",
        "summary_markdown",
        "outbox_summary_json",
    }

    # Native summary persisted as JSON + Markdown.
    saved = json.loads(Path(result.artifact_paths["recall_summary_json"]).read_text())
    assert saved["title"] == "Example project sync"

    # Notes saved with the recall_ai provider (Recall-native default source).
    notes = store.get_notes(meeting_id)
    assert notes is not None
    assert notes.model_provider == RECALL_SUMMARY_PROVIDER
    assert notes.executive_summary[0] == "Reviewed the partner pipeline."

    # Outbox summary payload queued as Telegram text only, with target metadata.
    outbox_path = Path(result.artifact_paths["outbox_summary_json"])
    assert outbox_path.name == SUMMARY_OUTBOX_FILENAME
    payload = json.loads(outbox_path.read_text())
    assert payload["kind"] == "meeting_summary"
    assert payload["chat_id"] == "-100123"
    assert payload["thread_id"] == "42"
    assert payload["attachments"] == []
    assert payload["status"] == "queued"
    assert "Example project sync" in payload["text"]


def test_ingest_recall_summary_safe_fallback_when_unavailable(tmp_path: Path):
    store = MeetingStore(tmp_path)
    meeting_id = "recall_bot_2"

    result = ingest_recall_summary(
        store,
        meeting_id=meeting_id,
        resource={"id": "tr_2", "transcript": []},
    )

    assert result.available is False
    assert result.artifact_paths == {}
    # No fabricated notes, no queued summary outbox.
    assert store.get_notes(meeting_id) is None
    assert not store.artifact_path(meeting_id, "outbox", SUMMARY_OUTBOX_FILENAME).exists()


def test_to_meeting_notes_roundtrips_through_storage(tmp_path: Path):
    store = MeetingStore(tmp_path)
    summary = extract_recall_summary(RESOURCE_WITH_SUMMARY)
    notes = summary.to_meeting_notes("m_1")
    store.save_notes(notes)
    reloaded = store.get_notes("m_1")
    assert reloaded.model_provider == RECALL_SUMMARY_PROVIDER
    assert reloaded.action_items[0]["task"] == "Send the recap"


def test_constants_exposed():
    assert RECALL_SUMMARY_JSON.endswith(".json")
    assert SUMMARY_MARKDOWN.endswith(".md")
    assert RECALL_SUMMARY_PROVIDER == "recall_ai"
