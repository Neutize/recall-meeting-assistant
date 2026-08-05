from __future__ import annotations

import json
from pathlib import Path

from recall_meeting_assistant.config import RecallMeetingsConfig
from recall_meeting_assistant.ingest import process_recall_webhook
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.webhooks import parse_event


class FakeRecallClient:
    def __init__(self) -> None:
        self.transcript_resource = {
            "id": "transcript_123",
            "recording": {"id": "recording_123"},
            "transcript": [
                {
                    "speaker": "Speaker A",
                    "start": 0,
                    "end": 2,
                    "text": "We agreed to publish the release notes tomorrow.",
                },
                {
                    "speaker": "Speaker B",
                    "start": 2,
                    "end": 4,
                    "text": "I will prepare the draft and share it with the team.",
                },
            ],
        }

    def get_recording_resource(self, resource_id: str, **kwargs):
        return self.transcript_resource

    def get_bot(self, bot_id: str):
        return {"id": bot_id}


def test_config_allows_transcript_only_install_without_optional_providers():
    config = RecallMeetingsConfig.from_env({"RECALLAI_API_KEY": "example-api-value"})

    assert config.recallai_api_key == "example-api-value"
    assert config.recallai_webhook_secret is None
    assert config.openai_api_key is None
    assert config.telegram_backend == "telegram"


def test_transcript_done_queues_the_full_transcript_for_delivery(tmp_path: Path):
    store = MeetingStore(tmp_path)
    event = parse_event(
        {
            "event": "transcript.done",
            "data": {
                "bot": {"id": "bot_123"},
                "recording": {"id": "recording_123"},
                "transcript": {"id": "transcript_123"},
            },
        }
    )

    result = process_recall_webhook(event, client=FakeRecallClient(), store=store)

    assert result.status.value == "completed"
    assert "outbox_transcript_json" in result.artifact_paths
    transcript_outbox = Path(result.artifact_paths["outbox_transcript_json"])
    payload = json.loads(transcript_outbox.read_text())
    assert payload["kind"] == "meeting_transcript"
    assert "Speaker A" in payload["text"]
    assert "Speaker B" in payload["text"]
    assert "release notes" in payload["text"]
