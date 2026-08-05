"""Tests for conservative participant mapping and Telegram formatting helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from recall_meeting_assistant.participants import (
    ALIAS_CONFIDENCE,
    EMAIL_CONFIDENCE,
    MATCH_REASON_ALIAS,
    MATCH_REASON_EMAIL,
    REASON_AMBIGUOUS,
    REASON_NO_INPUT,
    REASON_UNMATCHED,
    ParticipantDirectory,
    format_action_owner,
    format_mention,
)

PEOPLE = {
    "people": [
        {
            "canonical_name": "Person One",
            "emails": ["person.one@example.com"],
            "meet_aliases": ["Person One", "First Person"],
            "telegram": {"username": "person_one", "user_id": None},
            "tags": ["example", "marketing"],
        },
        {
            "canonical_name": "Person Two",
            "emails": ["person.two@example.com"],
            "meet_aliases": ["Person Two", "Second Person"],
            "telegram": {"username": None, "user_id": "123456"},
        },
        # Two different people share the alias "Alex" -> ambiguous.
        {"canonical_name": "Alex Public", "emails": ["alexp@example.com"], "meet_aliases": ["Alex"]},
        {"canonical_name": "Alex Quinn", "emails": ["alexq@example.com"], "meet_aliases": ["Alex"]},
    ]
}


@pytest.fixture()
def directory() -> ParticipantDirectory:
    return ParticipantDirectory.from_mapping(PEOPLE)


def test_exact_email_match_wins(directory: ParticipantDirectory):
    match = directory.match(email="  PERSON.ONE@EXAMPLE.COM ")
    assert match.matched is True
    assert match.canonical_name == "Person One"
    assert match.telegram_username == "person_one"
    assert match.reason == MATCH_REASON_EMAIL
    assert match.confidence == EMAIL_CONFIDENCE == 1.0
    assert match.is_taggable is True


def test_exact_alias_match_is_case_and_whitespace_insensitive(directory: ParticipantDirectory):
    match = directory.match(display_name="  first   person ")
    assert match.matched is True
    assert match.canonical_name == "Person One"
    assert match.reason == MATCH_REASON_ALIAS
    assert match.confidence == ALIAS_CONFIDENCE


def test_canonical_name_is_an_implicit_alias(directory: ParticipantDirectory):
    match = directory.match(display_name="Person Two")
    assert match.matched is True
    assert match.canonical_name == "Person Two"
    assert match.telegram_user_id == "123456"


def test_ambiguous_alias_is_not_tagged(directory: ParticipantDirectory):
    match = directory.match(display_name="Alex")
    assert match.matched is False
    assert match.reason == REASON_AMBIGUOUS
    assert match.is_taggable is False
    assert match.confidence == 0.0
    assert match.canonical_name is None


def test_unknown_display_name_is_unmatched(directory: ParticipantDirectory):
    match = directory.match(display_name="Some Stranger")
    assert match.matched is False
    assert match.reason == REASON_UNMATCHED


def test_no_input_is_a_distinct_reason(directory: ParticipantDirectory):
    match = directory.match()
    assert match.matched is False
    assert match.reason == REASON_NO_INPUT


def test_email_takes_precedence_over_ambiguous_display_name(directory: ParticipantDirectory):
    match = directory.match(display_name="Alex", email="person.two@example.com")
    assert match.matched is True
    assert match.canonical_name == "Person Two"
    assert match.reason == MATCH_REASON_EMAIL


def test_no_substring_or_fuzzy_matching(directory: ParticipantDirectory):
    assert directory.match(display_name="Person Twoish").matched is False
    assert directory.match(email="person.two@example").matched is False


def test_format_mention_prefers_username(directory: ParticipantDirectory):
    assert format_mention(directory.match(email="person.one@example.com")) == "@person_one"


def test_format_mention_uses_user_id_link_when_no_username(directory: ParticipantDirectory):
    assert (
        format_mention(directory.match(display_name="Person Two"))
        == "[Person Two](tg://user?id=123456)"
    )


def test_format_mention_unmatched_returns_fallback(directory: ParticipantDirectory):
    assert format_mention(directory.match(display_name="Ghost")) == "Unassigned"
    assert format_mention(directory.match(display_name="Ghost"), fallback="TBD") == "TBD"


def test_format_action_owner_tags_confident_match(directory: ParticipantDirectory):
    assert format_action_owner(directory, "Person One") == "@person_one"


def test_format_action_owner_shows_plain_name_when_not_taggable(directory: ParticipantDirectory):
    assert format_action_owner(directory, "Alex") == "Alex"
    assert format_action_owner(directory, "Some Stranger") == "Some Stranger"


def test_format_action_owner_unassigned_for_empty_or_unassigned(directory: ParticipantDirectory):
    assert format_action_owner(directory, "") == "Unassigned"
    assert format_action_owner(directory, None) == "Unassigned"
    assert format_action_owner(directory, "unassigned") == "Unassigned"


def test_from_file_loads_yaml(tmp_path: Path):
    path = tmp_path / "participants.yaml"
    path.write_text(
        "people:\n"
        "  - canonical_name: Person One\n"
        "    meet_aliases: [Person One]\n"
        "    telegram:\n"
        "      username: person_one\n"
    )
    directory = ParticipantDirectory.from_file(path)
    assert directory.match(display_name="Person One").telegram_username == "person_one"


def test_from_file_loads_json(tmp_path: Path):
    path = tmp_path / "participants.json"
    path.write_text(json.dumps(PEOPLE))
    directory = ParticipantDirectory.from_file(path)
    assert directory.match(email="person.two@example.com").canonical_name == "Person Two"


def test_from_file_missing_ok_returns_empty(tmp_path: Path):
    directory = ParticipantDirectory.from_file(tmp_path / "nope.yaml", missing_ok=True)
    assert directory.is_empty
    assert directory.match(display_name="Anyone").matched is False


def test_from_file_missing_raises_by_default(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        ParticipantDirectory.from_file(tmp_path / "nope.yaml")


def test_to_meeting_participant_carries_match_metadata(directory: ParticipantDirectory):
    participant = directory.to_meeting_participant(
        "recall_bot_1", display_name="Person One", recall_participant_id="100"
    )
    assert participant.meeting_id == "recall_bot_1"
    assert participant.canonical_name == "Person One"
    assert participant.telegram_username == "person_one"
    assert participant.match_confidence in {EMAIL_CONFIDENCE, ALIAS_CONFIDENCE}
    assert participant.match_reason == MATCH_REASON_ALIAS
    assert participant.recall_participant_id == "100"
