"""Process verified Recall webhook files into transcripts and outbox messages."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Iterable

from recall_meeting_assistant.client import RecallClient
from recall_meeting_assistant.config import RecallConfigError, RecallMeetingsConfig, load_config
from recall_meeting_assistant.ingest import process_recall_webhook
from recall_meeting_assistant.runtime import load_env_file, redact_sensitive_text
from recall_meeting_assistant.storage import MeetingStore
from recall_meeting_assistant.webhooks import parse_event


def load_processed(path: str | Path) -> set[str]:
    file_path = Path(path)
    if not file_path.is_file():
        return set()
    try:
        data = json.loads(file_path.read_text())
    except (OSError, json.JSONDecodeError):
        return set()
    if isinstance(data, list):
        return {str(item) for item in data}
    if isinstance(data, dict):
        return {str(item) for item in data.get("processed", [])}
    return set()


def save_processed(path: str | Path, processed: set[str]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(json.dumps({"processed": sorted(processed)}, indent=2) + "\n")
    temporary.replace(file_path)


def iter_webhook_files(directory: str | Path) -> list[Path]:
    path = Path(directory)
    return sorted(path.glob("*.json")) if path.is_dir() else []


def read_payload(path: str | Path) -> dict[str, Any] | None:
    try:
        document = json.loads(Path(path).read_text())
    except (OSError, json.JSONDecodeError):
        return None
    body = document.get("body", document) if isinstance(document, dict) else None
    return body if isinstance(body, dict) else None


def process_file(
    path: str | Path,
    client: RecallClient,
    store: MeetingStore,
    config: RecallMeetingsConfig,
) -> str:
    payload = read_payload(path)
    if payload is None:
        raise ValueError("webhook_file_invalid")
    event = parse_event(payload)
    result = process_recall_webhook(
        event,
        client=client,
        store=store,
        delivery_backend=config.telegram_backend,
        delivery_chat_id=config.telegram_chat_id,
        delivery_thread_id=config.telegram_thread_id,
        fallback_policy=config.fallback_policy,
        fallback_model=config.openai_fallback_model,
    )
    return f"processed {Path(path).name} {event.event_type or 'unknown'} {result.reason}"


def _safe_error(exc: Exception) -> str:
    return redact_sensitive_text(str(exc), force=True)[:240] or type(exc).__name__


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest stored Recall.ai webhooks")
    parser.add_argument("--webhook-dir", default=None, help="directory containing verified webhook JSON")
    parser.add_argument("--storage-dir", default=None, help="meeting artifact/storage directory")
    args = parser.parse_args(list(argv) if argv is not None else None)

    load_env_file()
    try:
        config = load_config()
    except RecallConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    webhook_dir = Path(args.webhook_dir or os.environ.get("MEETING_ASSISTANT_WEBHOOK_DIR", config.webhook_dir))
    storage_dir = Path(args.storage_dir or os.environ.get("MEETING_ASSISTANT_STORAGE_DIR", config.storage_dir))
    processed_path = config.processed_file
    if args.storage_dir and not os.environ.get("MEETING_ASSISTANT_PROCESSED_FILE"):
        processed_path = storage_dir / "ingest_processed.json"
    processed = load_processed(processed_path)
    files = iter_webhook_files(webhook_dir)
    if not files:
        return 0

    store = MeetingStore(storage_dir)
    client = RecallClient.from_config(config)
    messages: list[str] = []
    try:
        for path in files:
            key = str(path)
            if key in processed:
                continue
            try:
                messages.append(process_file(path, client, store, config))
            except Exception as exc:  # noqa: BLE001 - keep one bad event retryable
                messages.append(f"failed {path.name}: {_safe_error(exc)}")
                continue
            processed.add(key)
    finally:
        client.close()
        store.close()
    save_processed(processed_path, processed)
    if messages:
        print("\n".join(messages))
    return 0


__all__ = [
    "iter_webhook_files",
    "load_processed",
    "main",
    "process_file",
    "read_payload",
    "save_processed",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
