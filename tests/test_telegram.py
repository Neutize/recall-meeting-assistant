from __future__ import annotations

from typing import Any

from recall_meeting_assistant.telegram import make_telegram_sender, split_telegram_text


def test_split_telegram_text_preserves_all_content_under_limit():
    text = "line\n" * 2000
    chunks = split_telegram_text(text)
    assert len(chunks) > 1
    assert "".join(chunks) == text
    assert all(len(chunk) <= 4096 for chunk in chunks)


def test_telegram_sender_sends_long_transcript_in_chunks():
    calls: list[dict[str, Any]] = []

    def poster(*, token, method, payload):
        calls.append({"token": token, "method": method, "payload": payload})
        return {"ok": True, "result": {"message_id": len(calls)}}

    sender = make_telegram_sender(
        token="example-bot-token", default_chat_id=None, default_thread_id=None, poster=poster
    )
    message_ids = sender(
        text="line\n" * 2000,
        chat_id="-100123",
        thread_id="42",
        backend="telegram",
    )

    assert isinstance(message_ids, list)
    assert len(message_ids) == len(calls) > 1
    assert all(call["method"] == "sendMessage" for call in calls)
    assert all(call["payload"]["chat_id"] == "-100123" for call in calls)
    assert all(call["payload"]["message_thread_id"] == 42 for call in calls)


def test_telegram_sender_limits_rendered_html_after_escaping():
    calls: list[dict[str, Any]] = []

    def poster(*, token, method, payload):
        calls.append({"token": token, "method": method, "payload": payload})
        return {"ok": True, "result": {"message_id": len(calls)}}

    sender = make_telegram_sender(
        token="example-bot-token", default_chat_id="-100123", default_thread_id=None, poster=poster
    )
    sender(text="<" * 5000)

    assert len(calls) > 1
    assert all(len(call["payload"]["text"]) <= 4096 for call in calls)
