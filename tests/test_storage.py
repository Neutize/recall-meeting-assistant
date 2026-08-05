"""Tests for the Recall.ai meeting-assistant storage layer.

Covers Task 3 of docs/architecture.md:

* Create / update / get meeting sessions.
* Store and list participants, transcript segments, notes, action items, and
  Telegram delivery records.
* JSON-ish fields (raw transcript JSON, evidence segment ids, message ids,
  summary lists) round-trip safely.
* Timestamps persist as stable ISO-8601 UTC strings.
* Artifact paths always resolve *inside* the configured storage directory;
  traversal and absolute escapes are rejected.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from recall_meeting_assistant.models import (
    MeetingActionItem,
    MeetingParticipant,
    MeetingSession,
    TranscriptSegment,
)
from recall_meeting_assistant.storage import (
    ArtifactPathError,
    MeetingNotes,
    MeetingStore,
    TelegramDelivery,
)


@pytest.fixture()
def store(tmp_path):
    s = MeetingStore(tmp_path / "meetings")
    try:
        yield s
    finally:
        s.close()


def _session(meeting_id: str = "m_1", **overrides) -> MeetingSession:
    data = {
        "id": meeting_id,
        "recall_bot_id": "bot_123",
        "meeting_url_redacted": "https://meet.google.com/***",
        "title": "Example project sync",
    }
    data.update(overrides)
    return MeetingSession(**data)


# ── construction ─────────────────────────────────────────────────────────────


def test_init_creates_storage_dir(tmp_path):
    root = tmp_path / "nested" / "meetings"
    assert not root.exists()
    store = MeetingStore(root)
    try:
        assert root.is_dir()
        assert store.storage_dir == root
    finally:
        store.close()


def test_from_config_uses_storage_dir(tmp_path):
    class _Cfg:
        storage_dir = tmp_path / "cfg_meetings"

    store = MeetingStore.from_config(_Cfg())
    try:
        assert store.storage_dir == tmp_path / "cfg_meetings"
        assert store.storage_dir.is_dir()
    finally:
        store.close()


# ── meeting sessions ─────────────────────────────────────────────────────────


def test_create_and_get_session(store):
    store.create_session(_session())

    got = store.get_session("m_1")
    assert got is not None
    assert got.id == "m_1"
    assert got.recall_bot_id == "bot_123"
    assert got.meeting_url_redacted == "https://meet.google.com/***"
    assert got.external_provider == "recall_ai"
    # Auto-stamped on create.
    assert got.created_at is not None
    assert got.updated_at is not None


def test_get_missing_session_returns_none(store):
    assert store.get_session("nope") is None


def test_create_session_returns_session_with_timestamps(store):
    created = store.create_session(_session())
    assert created.created_at is not None
    assert created.created_at.tzinfo is not None
    assert created.created_at.utcoffset() == timezone.utc.utcoffset(None)


def test_update_session_changes_fields(store):
    store.create_session(_session())

    updated = store.update_session(
        "m_1", status="done", transcript_status="completed", fallback_used=True
    )
    assert updated.status == "done"
    assert updated.transcript_status == "completed"
    assert updated.fallback_used is True

    reloaded = store.get_session("m_1")
    assert reloaded.status == "done"
    assert reloaded.fallback_used is True


def test_update_session_bumps_updated_at(store):
    created = store.create_session(
        _session(updated_at=datetime(2020, 1, 1, tzinfo=timezone.utc))
    )
    updated = store.update_session("m_1", status="recording")
    assert updated.updated_at > created.updated_at


def test_update_missing_session_raises(store):
    with pytest.raises(KeyError):
        store.update_session("ghost", status="done")


def test_create_session_is_idempotent_via_update(store):
    store.create_session(_session(title="First"))
    # Re-creating with same id should not crash and should reflect new data.
    store.create_session(_session(title="Second"))
    assert store.get_session("m_1").title == "Second"


def test_list_sessions(store):
    store.create_session(_session("m_1"))
    store.create_session(_session("m_2", title="Other"))

    ids = {s.id for s in store.list_sessions()}
    assert ids == {"m_1", "m_2"}


# ── participants ─────────────────────────────────────────────────────────────


def test_add_and_list_participants(store):
    store.create_session(_session())

    p1 = store.add_participant(
        MeetingParticipant(meeting_id="m_1", raw_display_name="Person One", email="person.one@example.com")
    )
    store.add_participant(
        MeetingParticipant(meeting_id="m_1", raw_display_name="Alex")
    )

    assert p1.id  # storage assigns an id
    listed = store.list_participants("m_1")
    assert [p.raw_display_name for p in listed] == ["Person One", "Alex"]
    assert listed[0].email == "person.one@example.com"


def test_list_participants_scoped_to_meeting(store):
    store.create_session(_session("m_1"))
    store.create_session(_session("m_2"))
    store.add_participant(MeetingParticipant(meeting_id="m_1", raw_display_name="A"))
    store.add_participant(MeetingParticipant(meeting_id="m_2", raw_display_name="B"))

    assert [p.raw_display_name for p in store.list_participants("m_1")] == ["A"]


# ── transcript segments ──────────────────────────────────────────────────────


def test_add_and_list_transcript_segments_ordered(store):
    store.create_session(_session())

    store.add_transcript_segment(
        TranscriptSegment(
            meeting_id="m_1", segment_index=1, start_ms=1500, end_ms=3000, text="world"
        )
    )
    store.add_transcript_segment(
        TranscriptSegment(
            meeting_id="m_1", segment_index=0, start_ms=0, end_ms=1500, text="hello"
        )
    )

    segs = store.list_transcript_segments("m_1")
    assert [s.text for s in segs] == ["hello", "world"]
    assert [s.segment_index for s in segs] == [0, 1]


def test_transcript_segment_raw_json_roundtrips(store):
    store.create_session(_session())
    raw = {"words": [{"t": "hi", "start": 0.0}], "speaker": {"id": 7}}
    store.add_transcript_segment(
        TranscriptSegment(
            meeting_id="m_1",
            segment_index=0,
            start_ms=0,
            end_ms=900,
            text="hi",
            speaker_raw="Speaker 1",
            confidence=0.92,
            raw_json=raw,
        )
    )

    seg = store.list_transcript_segments("m_1")[0]
    assert seg.raw_json == raw
    assert seg.speaker_raw == "Speaker 1"
    assert seg.confidence == pytest.approx(0.92)


def test_add_transcript_segments_bulk(store):
    store.create_session(_session())
    segs = [
        TranscriptSegment(meeting_id="m_1", segment_index=i, start_ms=i, end_ms=i + 1, text=str(i))
        for i in range(3)
    ]
    store.add_transcript_segments(segs)
    assert len(store.list_transcript_segments("m_1")) == 3


# ── notes ────────────────────────────────────────────────────────────────────


def test_save_and_get_notes(store):
    store.create_session(_session())
    notes = MeetingNotes(
        meeting_id="m_1",
        executive_summary=["Discussed BD pipeline", "Agreed on timeline"],
        executive_summary_markdown="# Summary\n- Discussed BD pipeline",
        decisions=[{"text": "Ship Friday", "evidence_segment_ids": ["s1"]}],
        open_questions=[{"question": "Budget?", "owner": "Person One"}],
        risks=[{"risk": "Vendor delay", "severity": "medium"}],
        model_provider="openai",
        model_name="gpt-4o",
    )

    saved = store.save_notes(notes)
    assert saved.id

    got = store.get_notes("m_1")
    assert got is not None
    assert got.executive_summary == ["Discussed BD pipeline", "Agreed on timeline"]
    assert got.decisions[0]["text"] == "Ship Friday"
    assert got.open_questions[0]["question"] == "Budget?"
    assert got.risks[0]["severity"] == "medium"
    assert got.model_name == "gpt-4o"
    assert got.created_at is not None


def test_get_notes_missing_returns_none(store):
    assert store.get_notes("m_1") is None


def test_save_notes_returns_latest(store):
    store.create_session(_session())
    store.save_notes(MeetingNotes(meeting_id="m_1", executive_summary=["v1"]))
    store.save_notes(MeetingNotes(meeting_id="m_1", executive_summary=["v2"]))
    assert store.get_notes("m_1").executive_summary == ["v2"]


# ── action items ─────────────────────────────────────────────────────────────


def test_add_and_list_action_items(store):
    store.create_session(_session())

    item = store.add_action_item(
        MeetingActionItem(
            meeting_id="m_1",
            task="Send recap",
            owner_canonical="person_one",
            evidence_segment_ids=["s1", "s2"],
        )
    )
    assert item.id

    items = store.list_action_items("m_1")
    assert len(items) == 1
    assert items[0].task == "Send recap"
    assert items[0].owner_canonical == "person_one"
    assert items[0].evidence_segment_ids == ["s1", "s2"]
    assert items[0].status == "open"


def test_action_item_defaults_unassigned(store):
    store.create_session(_session())
    store.add_action_item(MeetingActionItem(meeting_id="m_1", task="Follow up"))
    assert store.list_action_items("m_1")[0].owner_canonical == "unassigned"


# ── telegram deliveries ──────────────────────────────────────────────────────


def test_add_and_list_deliveries(store):
    store.create_session(_session())

    rec = store.add_delivery(
        TelegramDelivery(
            meeting_id="m_1",
            backend="telegram",
            chat_id="-100123",
            thread_id="42",
            message_ids=["1001", "1002"],
        )
    )
    assert rec.id
    assert rec.delivered_at is not None  # stamped on insert

    listed = store.list_deliveries("m_1")
    assert len(listed) == 1
    assert listed[0].backend == "telegram"
    assert listed[0].message_ids == ["1001", "1002"]
    assert listed[0].chat_id == "-100123"


def test_delivery_error_record(store):
    store.create_session(_session())
    store.add_delivery(
        TelegramDelivery(meeting_id="m_1", backend="telegram", error="rate limited")
    )
    rec = store.list_deliveries("m_1")[0]
    assert rec.error == "rate limited"
    assert rec.message_ids == []


# ── persistence across instances ─────────────────────────────────────────────


def test_data_persists_across_instances(tmp_path):
    root = tmp_path / "meetings"
    first = MeetingStore(root)
    first.create_session(_session())
    first.add_participant(MeetingParticipant(meeting_id="m_1", raw_display_name="Person One"))
    first.close()

    second = MeetingStore(root)
    try:
        assert second.get_session("m_1") is not None
        assert len(second.list_participants("m_1")) == 1
    finally:
        second.close()


# ── artifact path safety ─────────────────────────────────────────────────────


def test_artifact_path_under_storage_dir(store):
    path = store.artifact_path("m_1", "transcript.md")
    assert path == store.storage_dir / "m_1" / "transcript.md"
    assert store.storage_dir in path.parents


def test_artifact_path_multiple_parts(store):
    path = store.artifact_path("m_1", "audio", "clip.wav")
    assert path == store.storage_dir / "m_1" / "audio" / "clip.wav"


def test_artifact_path_create_parents(store):
    path = store.artifact_path("m_1", "audio", "clip.wav", create_parents=True)
    assert path.parent.is_dir()


def test_artifact_path_rejects_parent_traversal(store):
    with pytest.raises(ArtifactPathError):
        store.artifact_path("m_1", "../../etc/passwd")


def test_artifact_path_rejects_absolute_part(store):
    with pytest.raises(ArtifactPathError):
        store.artifact_path("m_1", "/etc/passwd")


def test_artifact_path_rejects_meeting_id_traversal(store):
    with pytest.raises(ArtifactPathError):
        store.artifact_path("../evil", "x.txt")


def test_artifact_path_rejects_embedded_traversal(store):
    with pytest.raises(ArtifactPathError):
        store.artifact_path("m_1", "sub/../../escape.txt")


def test_artifact_path_requires_meeting_id(store):
    with pytest.raises(ValueError):
        store.artifact_path("   ", "x.txt")


# ── timestamp hygiene ────────────────────────────────────────────────────────


def test_session_timestamps_stored_as_iso_utc(store):
    store.create_session(_session())
    raw = store.connection.execute(
        "SELECT created_at, updated_at FROM meeting_sessions WHERE id = ?", ("m_1",)
    ).fetchone()
    created_at = raw[0]
    assert isinstance(created_at, str)
    # Parseable ISO-8601 and normalized to UTC ("Z" or +00:00).
    assert created_at.endswith("Z") or created_at.endswith("+00:00")
    parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
