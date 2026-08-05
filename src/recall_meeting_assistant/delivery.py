"""Provider-agnostic outbox delivery adapter.

The product core writes JSON outbox files. This module consumes those files using
an injectable sender callable and records durable delivered/failed state. The
Telegram implementation lives in ``recall_meeting_assistant.telegram``.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol

from recall_meeting_assistant.storage import MeetingStore, TelegramDelivery

DELIVERED_STATUS = "delivered"
FAILED_STATUS = "failed"
QUEUED_STATUS = "queued"


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class OutboxSender(Protocol):
    def __call__(
        self,
        *,
        text: str,
        chat_id: str | None = None,
        thread_id: str | None = None,
        backend: str | None = None,
        attachments: list[str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str | int | list[str | int] | None: ...


@dataclass(frozen=True)
class DeliveryOutcome:
    path: str
    meeting_id: str
    kind: str
    status: str
    message_ids: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class OutboxPayload:
    path: Path
    meeting_id: str
    kind: str
    text: str
    backend: str = "telegram"
    chat_id: str | None = None
    thread_id: str | None = None
    attachments: list[str] = field(default_factory=list)
    status: str = QUEUED_STATUS
    attempts: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_file(cls, path: str | Path) -> "OutboxPayload":
        file_path = Path(path)
        data = json.loads(file_path.read_text())
        if not isinstance(data, dict):
            raise ValueError(f"Outbox file must contain an object: {file_path}")
        meeting_id = str(data.get("meeting_id") or "").strip()
        kind = str(data.get("kind") or "").strip()
        text = str(data.get("text") or "").strip()
        if not meeting_id or not kind or not text:
            raise ValueError(f"Outbox file missing required fields: {file_path}")
        attachments = [str(item) for item in (data.get("attachments") or [])]
        return cls(
            path=file_path,
            meeting_id=meeting_id,
            kind=kind,
            text=text,
            backend=str(data.get("backend") or "telegram"),
            chat_id=_clean_optional(data.get("chat_id")),
            thread_id=_clean_optional(data.get("thread_id")),
            attachments=attachments,
            status=str(data.get("status") or QUEUED_STATUS),
            attempts=int(data.get("attempts") or 0),
            raw=data,
        )


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _message_ids(value: str | int | list[str | int] | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]


def _write_state(payload: OutboxPayload, *, status: str, message_ids: list[str] | None = None, error: str | None = None) -> None:
    data = dict(payload.raw)
    data.update(
        {
            "status": status,
            "attempts": payload.attempts + 1,
            "updated_at": _utcnow_iso(),
        }
    )
    if status == DELIVERED_STATUS:
        data["delivered_at"] = _utcnow_iso()
        data["message_ids"] = list(message_ids or [])
        data.pop("error", None)
    elif status == FAILED_STATUS:
        data["failed_at"] = _utcnow_iso()
        data["error"] = (error or "delivery_failed")[:500]
    tmp = payload.path.with_suffix(payload.path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    tmp.replace(payload.path)


def discover_outbox_files(root: str | Path) -> list[Path]:
    """Return queued outbox JSON files under a storage root or a single file path."""

    root_path = Path(root)
    if root_path.is_file():
        return [root_path]
    return sorted(root_path.glob("*/outbox/*.json"))


def deliver_outbox_file(
    path: str | Path,
    *,
    sender: OutboxSender,
    store: MeetingStore | None = None,
    skip_delivered: bool = True,
) -> DeliveryOutcome:
    payload = OutboxPayload.from_file(path)
    if skip_delivered and payload.status == DELIVERED_STATUS:
        return DeliveryOutcome(
            path=str(payload.path),
            meeting_id=payload.meeting_id,
            kind=payload.kind,
            status=DELIVERED_STATUS,
            message_ids=[str(m) for m in payload.raw.get("message_ids") or []],
        )
    if payload.status not in {QUEUED_STATUS, FAILED_STATUS} and skip_delivered:
        return DeliveryOutcome(
            path=str(payload.path),
            meeting_id=payload.meeting_id,
            kind=payload.kind,
            status=payload.status,
        )
    try:
        message_id_result = sender(
            text=payload.text,
            chat_id=payload.chat_id,
            thread_id=payload.thread_id,
            backend=payload.backend,
            attachments=payload.attachments,
            payload=payload.raw,
        )
        message_ids = _message_ids(message_id_result)
        _write_state(payload, status=DELIVERED_STATUS, message_ids=message_ids)
        if store is not None:
            store.add_delivery(
                TelegramDelivery(
                    meeting_id=payload.meeting_id,
                    backend=payload.backend,
                    chat_id=payload.chat_id,
                    thread_id=payload.thread_id,
                    message_ids=message_ids,
                )
            )
            existing = store.get_session(payload.meeting_id)
            if existing is not None:
                store.update_session(payload.meeting_id, telegram_delivery_status=DELIVERED_STATUS)
        return DeliveryOutcome(
            path=str(payload.path),
            meeting_id=payload.meeting_id,
            kind=payload.kind,
            status=DELIVERED_STATUS,
            message_ids=message_ids,
        )
    except Exception as exc:  # noqa: BLE001 - preserve retryable failure state
        # Provider exceptions may contain tokens, signed URLs, or request data.
        # Keep the persisted retry reason typed but deliberately generic.
        error = f"{type(exc).__name__}: delivery_failed"
        _write_state(payload, status=FAILED_STATUS, error=error)
        if store is not None:
            existing = store.get_session(payload.meeting_id)
            if existing is not None:
                store.update_session(payload.meeting_id, telegram_delivery_status=FAILED_STATUS)
        return DeliveryOutcome(
            path=str(payload.path),
            meeting_id=payload.meeting_id,
            kind=payload.kind,
            status=FAILED_STATUS,
            error=error,
        )


def deliver_outbox(
    root: str | Path,
    *,
    sender: OutboxSender,
    store: MeetingStore | None = None,
    skip_delivered: bool = True,
) -> list[DeliveryOutcome]:
    return [
        deliver_outbox_file(path, sender=sender, store=store, skip_delivered=skip_delivered)
        for path in discover_outbox_files(root)
    ]


def fake_sender(
    *,
    text: str,
    chat_id: str | None = None,
    thread_id: str | None = None,
    backend: str | None = None,
    attachments: list[str] | None = None,
    payload: Mapping[str, Any] | None = None,
) -> str:
    """Deterministic sender for smoke tests and local dry runs."""

    _ = (text, chat_id, thread_id, backend, attachments, payload)
    return "fake-message-id"


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Consume meeting-assistant outbox files")
    parser.add_argument("root", help="storage root or outbox JSON file")
    parser.add_argument("--fake", action="store_true", help="use deterministic fake sender for smoke tests")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if not args.fake:
        raise SystemExit("Use --fake for a local smoke test, or the Telegram delivery command for live sends.")
    outcomes = deliver_outbox(args.root, sender=fake_sender)
    print(json.dumps([outcome.__dict__ for outcome in outcomes], ensure_ascii=False, indent=2))
    return 0 if all(outcome.status == DELIVERED_STATUS for outcome in outcomes) else 1


__all__ = [
    "DELIVERED_STATUS",
    "FAILED_STATUS",
    "QUEUED_STATUS",
    "DeliveryOutcome",
    "OutboxPayload",
    "discover_outbox_files",
    "deliver_outbox",
    "deliver_outbox_file",
    "fake_sender",
    "main",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
