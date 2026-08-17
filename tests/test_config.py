"""Tests for portable configuration and model redaction."""

from __future__ import annotations

from pathlib import Path

import pytest

from recall_meeting_assistant.config import (
    DEFAULT_OPENAI_FALLBACK_MODEL,
    DEFAULT_REGION,
    RecallConfigError,
    RecallMeetingsConfig,
    load_config,
)
from recall_meeting_assistant.runtime import get_product_home

# Deliberately non-production test values. They are not credentials.
FAKE_API_VALUE = "example-api-value"
FAKE_WEBHOOK_VALUE = "example-webhook-value"
FAKE_OPENAI_VALUE = "example-provider-value"
ALL_VALUES = (FAKE_API_VALUE, FAKE_WEBHOOK_VALUE, FAKE_OPENAI_VALUE)


def _full_env(**overrides: str) -> dict[str, str]:
    env = {
        "RECALLAI_API_KEY": FAKE_API_VALUE,
        "RECALLAI_WEBHOOK_SECRET": FAKE_WEBHOOK_VALUE,
        "OPENAI_API_KEY": FAKE_OPENAI_VALUE,
        "MEETING_ASSISTANT_PUBLIC_BASE_URL": "https://hooks.example.com/recall",
    }
    env.update(overrides)
    return env


def test_from_env_loads_values():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    assert cfg.recallai_api_key == FAKE_API_VALUE
    assert cfg.recallai_webhook_secret == FAKE_WEBHOOK_VALUE
    assert cfg.openai_api_key == FAKE_OPENAI_VALUE
    assert cfg.public_base_url == "https://hooks.example.com/recall"


def test_transcript_only_install_needs_only_recall_key():
    cfg = RecallMeetingsConfig.from_env({"RECALLAI_API_KEY": FAKE_API_VALUE})
    assert cfg.recallai_api_key == FAKE_API_VALUE
    assert cfg.recallai_webhook_secret is None
    assert cfg.openai_api_key is None


def test_webhook_receiver_can_require_a_secret():
    with pytest.raises(RecallConfigError) as exc_info:
        RecallMeetingsConfig.from_env(
            {"RECALLAI_API_KEY": FAKE_API_VALUE}, require_webhook_secret=True
        )
    assert "RECALLAI_WEBHOOK_SECRET or RECALL_WEBHOOK_TOKEN" in exc_info.value.missing


def test_region_defaults_when_absent():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    assert cfg.recallai_region == DEFAULT_REGION == "us-east-1"


def test_region_override():
    cfg = RecallMeetingsConfig.from_env(_full_env(RECALLAI_REGION="eu-west-1"))
    assert cfg.recallai_region == "eu-west-1"


def test_optional_fields_default_to_none():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    assert cfg.telegram_chat_id is None
    assert cfg.telegram_thread_id is None
    assert cfg.telegram_bot_token is None
    assert cfg.monthly_cost_cap_usd is None


def test_non_secret_defaults():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    assert cfg.telegram_backend == "telegram"
    assert cfg.openai_fallback_model == DEFAULT_OPENAI_FALLBACK_MODEL
    assert cfg.fallback_policy == "auto"
    assert cfg.allowed_meeting_domains == ("meet.google.com",)
    assert cfg.bot_name == "Meeting Notetaker"
    assert cfg.language_code == "auto"


def test_storage_paths_default_under_product_home():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    home = get_product_home()
    assert cfg.storage_dir == home / "meetings"
    assert cfg.webhook_dir == home / "webhooks"
    assert cfg.participant_map_path == home / "participants.yaml"


def test_storage_dir_override():
    cfg = RecallMeetingsConfig.from_env(_full_env(MEETING_ASSISTANT_STORAGE_DIR="/srv/meetings"))
    assert cfg.storage_dir == Path("/srv/meetings")
    assert cfg.processed_file == Path("/srv/meetings/ingest_processed.json")


def test_allowed_domains_parsed_and_normalized():
    cfg = RecallMeetingsConfig.from_env(
        _full_env(MEETING_ASSISTANT_ALLOWED_DOMAINS=" Meet.Google.com , zoom.us ,")
    )
    assert cfg.allowed_meeting_domains == ("meet.google.com", "zoom.us")


def test_cost_cap_parsed_as_float():
    cfg = RecallMeetingsConfig.from_env(
        _full_env(MEETING_ASSISTANT_MONTHLY_COST_CAP_USD="42.50")
    )
    assert cfg.monthly_cost_cap_usd == pytest.approx(42.50)


def test_load_config_reads_process_env(monkeypatch):
    for key, value in _full_env().items():
        monkeypatch.setenv(key, value)
    cfg = load_config()
    assert cfg.recallai_api_key == FAKE_API_VALUE


def test_missing_api_key_raises_actionable_error():
    env = _full_env()
    del env["RECALLAI_API_KEY"]
    with pytest.raises(RecallConfigError) as exc_info:
        RecallMeetingsConfig.from_env(env)
    err = exc_info.value
    assert "RECALLAI_API_KEY" in err.missing
    assert "RECALLAI_API_KEY" in str(err)
    assert ".env" in str(err)


def test_whitespace_only_value_treated_as_missing():
    with pytest.raises(RecallConfigError) as exc_info:
        RecallMeetingsConfig.from_env(_full_env(RECALLAI_API_KEY="   "))
    assert "RECALLAI_API_KEY" in exc_info.value.missing


def test_error_message_never_contains_present_values():
    env = _full_env()
    del env["RECALLAI_API_KEY"]
    with pytest.raises(RecallConfigError) as exc_info:
        RecallMeetingsConfig.from_env(env)
    message = str(exc_info.value)
    assert FAKE_WEBHOOK_VALUE not in message
    assert FAKE_OPENAI_VALUE not in message


def test_invalid_backend_raises():
    with pytest.raises(RecallConfigError) as exc_info:
        RecallMeetingsConfig.from_env(
            _full_env(MEETING_ASSISTANT_TELEGRAM_BACKEND="carrier_pigeon")
        )
    assert "MEETING_ASSISTANT_TELEGRAM_BACKEND" in str(exc_info.value)


def test_repr_does_not_leak_values():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    text = repr(cfg)
    for value in ALL_VALUES:
        assert value not in text
    assert "us-east-1" in text


def test_str_does_not_leak_values():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    for value in ALL_VALUES:
        assert value not in str(cfg)


def test_as_redacted_dict_masks_values():
    cfg = RecallMeetingsConfig.from_env(_full_env())
    redacted = cfg.as_redacted_dict()
    for value in ALL_VALUES:
        assert value not in str(redacted)
    assert redacted["recallai_region"] == "us-east-1"
    assert redacted["public_base_url"] == "https://hooks.example.com/recall"


def test_diagnostics_do_not_leak_delivery_destination():
    cfg = RecallMeetingsConfig.from_env(
        _full_env(
            MEETING_ASSISTANT_TELEGRAM_CHAT_ID="-1001234567890",
            MEETING_ASSISTANT_TELEGRAM_THREAD_ID="42",
        )
    )
    assert "-1001234567890" not in repr(cfg)
    assert "-1001234567890" not in str(cfg.as_redacted_dict())
    assert "42" not in str(cfg.as_redacted_dict()["telegram_thread_id"])


def test_loading_does_not_print_values(capsys):
    RecallMeetingsConfig.from_env(_full_env())
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    for value in ALL_VALUES:
        assert value not in combined


def test_models_meeting_session_roundtrip():
    from recall_meeting_assistant.models import MeetingSession

    session = MeetingSession(
        id="m_1",
        recall_bot_id="bot_123",
        meeting_url_redacted="https://meet.google.com/***",
        title="Example team sync",
    )
    restored = MeetingSession.from_dict(session.to_dict())
    assert restored.id == "m_1"
    assert restored.recall_bot_id == "bot_123"
    assert restored.external_provider == "recall_ai"


def test_models_redact_meeting_url_strips_meeting_code():
    from recall_meeting_assistant.models import redact_meeting_url

    redacted = redact_meeting_url("https://meet.google.com/example-meeting")
    assert "example-meeting" not in redacted
    assert "meet.google.com" in redacted


def test_models_transcript_segment_roundtrip():
    from recall_meeting_assistant.models import TranscriptSegment

    seg = TranscriptSegment(
        meeting_id="m_1",
        segment_index=0,
        start_ms=0,
        end_ms=1500,
        speaker_raw="Speaker 1",
        text="Hello team",
        provider="recallai_streaming",
    )
    restored = TranscriptSegment.from_dict(seg.to_dict())
    assert restored.text == "Hello team"
    assert restored.segment_index == 0


def test_models_action_item_defaults():
    from recall_meeting_assistant.models import MeetingActionItem

    item = MeetingActionItem(meeting_id="m_1", task="Send recap")
    assert item.owner_canonical == "unassigned"
    assert item.status == "open"
    restored = MeetingActionItem.from_dict(item.to_dict())
    assert restored.task == "Send recap"
