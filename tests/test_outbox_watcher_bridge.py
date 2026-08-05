"""Tests for the standalone Telegram outbox delivery bridge.

The bridge is kept as a thin checkout-compatible script. Tests load the repo
copy by default and can be pointed at another copy with RECALL_OUTBOX_WATCHER_PATH.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

from recall_meeting_assistant.delivery import DELIVERED_STATUS
from recall_meeting_assistant.outbox import (
    queue_left_meeting_notification,
    queue_summary_notification,
)
from recall_meeting_assistant.storage import MeetingStore

WATCHER_PATH = Path(
    os.environ.get(
        "RECALL_OUTBOX_WATCHER_PATH",
        str(Path(__file__).resolve().parents[1] / "scripts" / "recall_meeting_outbox_watcher.py"),
    )
)


def _load_watcher():
    if not WATCHER_PATH.is_file():
        pytest.skip(f"outbox watcher bridge not present at {WATCHER_PATH}")
    spec = importlib.util.spec_from_file_location("recall_outbox_watcher", WATCHER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _record_poster():
    sent: list[dict] = []

    def poster(token, method, payload):
        sent.append({"token_present": bool(token), "method": method, "payload": dict(payload)})
        return {"ok": True, "result": {"message_id": 4242}}

    return poster, sent


def test_sender_targets_topic_and_returns_message_id():
    watcher = _load_watcher()
    poster, sent = _record_poster()
    sender = watcher.make_telegram_sender(
        token="test-token",
        default_chat_id="-1001234567890",
        default_thread_id="42",
        poster=poster,
    )

    message_id = sender(text="**Marketing**: A&B", chat_id=None, thread_id=None)

    assert message_id == "4242"
    assert sent[0]["method"] == "sendMessage"
    assert sent[0]["payload"]["chat_id"] == "-1001234567890"
    assert sent[0]["payload"]["message_thread_id"] == 42  # coerced to int for the API
    assert sent[0]["payload"]["parse_mode"] == "HTML"
    assert sent[0]["payload"]["text"] == "<b>Marketing</b>: A&amp;B"
    assert "test-token" not in json.dumps(sent[0])  # token never enters the payload


def test_payload_destination_overrides_defaults():
    watcher = _load_watcher()
    poster, sent = _record_poster()
    sender = watcher.make_telegram_sender(
        token="t", default_chat_id="-100default", default_thread_id="1", poster=poster
    )

    sender(text="x", chat_id="-100explicit", thread_id="9")

    assert sent[0]["payload"]["chat_id"] == "-100explicit"
    assert sent[0]["payload"]["message_thread_id"] == 9


def test_run_delivers_pending_and_marks_state(tmp_path: Path):
    watcher = _load_watcher()
    store = MeetingStore(tmp_path)
    outbox_path = Path(queue_left_meeting_notification(store, meeting_id="recall_bot_1"))
    store.close()
    poster, sent = _record_poster()
    out, err = io.StringIO(), io.StringIO()

    code = watcher.run(
        storage_root=tmp_path,
        backend="telegram",
        chat_id="-1001234567890",
        thread_id="42",
        token="tok",
        poster=poster,
        out=out,
        err=err,
    )

    assert code == 0
    assert len(sent) == 1
    assert json.loads(outbox_path.read_text())["status"] == DELIVERED_STATUS
    assert "delivered recall_bot_1 meeting_left" in out.getvalue()
    assert err.getvalue() == ""


def test_run_is_silent_and_skips_when_nothing_pending(tmp_path: Path):
    watcher = _load_watcher()
    store = MeetingStore(tmp_path)
    outbox_path = Path(queue_summary_notification(store, meeting_id="recall_bot_2", text="done"))
    payload = json.loads(outbox_path.read_text())
    payload["status"] = DELIVERED_STATUS
    outbox_path.write_text(json.dumps(payload))
    store.close()
    poster, sent = _record_poster()
    out, err = io.StringIO(), io.StringIO()

    code = watcher.run(
        storage_root=tmp_path,
        backend="telegram",
        chat_id="-100",
        thread_id="1",
        token="tok",
        poster=poster,
        out=out,
        err=err,
    )

    assert code == 0
    assert sent == []  # already delivered -> no duplicate send
    assert out.getvalue() == ""  # quiet for cron


def test_dry_run_does_not_send_or_mutate(tmp_path: Path):
    watcher = _load_watcher()
    store = MeetingStore(tmp_path)
    outbox_path = Path(queue_left_meeting_notification(store, meeting_id="recall_bot_3"))
    store.close()
    before = outbox_path.read_text()
    poster, sent = _record_poster()
    out = io.StringIO()

    code = watcher.run(
        storage_root=tmp_path,
        backend="telegram",
        chat_id="-100",
        thread_id="1",
        token="tok",
        dry_run=True,
        poster=poster,
        out=out,
    )

    assert code == 0
    assert sent == []
    assert outbox_path.read_text() == before  # untouched
    assert "DRY-RUN would send recall_bot_3" in out.getvalue()


def test_load_env_file_does_not_override_or_leak(tmp_path: Path):
    watcher = _load_watcher()
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# comment\nTELEGRAM_BOT_TOKEN="example-token"\nFOO_NEW_KEY=value\nEMPTY=\n'
    )
    os.environ.pop("FOO_NEW_KEY", None)
    os.environ["TELEGRAM_BOT_TOKEN"] = "preexisting"
    try:
        applied = watcher.load_env_file(env_file)
        assert os.environ["TELEGRAM_BOT_TOKEN"] == "preexisting"  # env wins, not overridden
        assert os.environ["FOO_NEW_KEY"] == "value"
        assert applied["FOO_NEW_KEY"] == "set"  # returns only "set", never the value
        assert "example-token" not in json.dumps(applied)
    finally:
        os.environ.pop("FOO_NEW_KEY", None)
        os.environ.pop("TELEGRAM_BOT_TOKEN", None)
