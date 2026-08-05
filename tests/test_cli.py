from __future__ import annotations

import pytest

from recall_meeting_assistant import cli
from recall_meeting_assistant.client import RecallBot


def test_validate_meeting_url_requires_https_and_allowlisted_host():
    assert cli.validate_meeting_url("https://meet.google.com/abc-defg-hij", ("meet.google.com",))
    with pytest.raises(ValueError):
        cli.validate_meeting_url("http://meet.google.com/abc-defg-hij", ("meet.google.com",))
    with pytest.raises(ValueError):
        cli.validate_meeting_url("https://evil.example/meeting", ("meet.google.com",))


def test_create_bot_command_prints_safe_result_without_network(monkeypatch, capsys):
    monkeypatch.setenv("RECALLAI_API_KEY", "example-api-value")
    monkeypatch.setenv("MEETING_ASSISTANT_ALLOWED_DOMAINS", "meet.google.com")

    def fake_create(self, meeting_url, **kwargs):
        assert meeting_url.startswith("https://meet.google.com/")
        return RecallBot(id="bot_example", status="joining_call")

    monkeypatch.setattr("recall_meeting_assistant.client.RecallClient.create_bot", fake_create)
    assert cli.main(["create-bot", "https://meet.google.com/abc-defg-hij"]) == 0
    output = capsys.readouterr().out
    assert "bot_example" in output
    assert "meet.google.com/abc" not in output
