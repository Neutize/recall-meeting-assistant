"""Telegram delivery adapter for queued meeting-assistant messages."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from recall_meeting_assistant.delivery import (
    DELIVERED_STATUS,
    FAILED_STATUS,
    QUEUED_STATUS,
    OutboxPayload,
    deliver_outbox_file,
    discover_outbox_files,
)
from recall_meeting_assistant.runtime import get_product_home, load_env_file
from recall_meeting_assistant.storage import MeetingStore

DEFAULT_STORAGE_DIR = str(get_product_home() / "meetings")
DEFAULT_BACKEND = "telegram"
TELEGRAM_TEXT_LIMIT = 4096
PENDING_STATUSES = {QUEUED_STATUS, FAILED_STATUS}
_BOLD_MARKDOWN_RE = re.compile(r"\*\*([^*\n][\s\S]*?[^*\n])\*\*")


def _coerce_thread_id(thread_id: str | None) -> int | None:
    if thread_id is None:
        return None
    text = str(thread_id).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def telegram_markdown_to_html(text: str) -> str:
    """Convert the product's small Markdown subset to Telegram HTML safely."""

    escaped = html.escape(text, quote=False)
    return _BOLD_MARKDOWN_RE.sub(r"<b>\1</b>", escaped)


def split_telegram_text(text: str, *, limit: int = TELEGRAM_TEXT_LIMIT) -> list[str]:
    """Split text on line boundaries so long transcripts are not truncated."""

    if limit < 1:
        raise ValueError("Telegram message limit must be positive.")
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        candidate = remaining[:limit]
        split_at = candidate.rfind("\n")
        if split_at < max(1, limit // 3):
            split_at = candidate.rfind(" ")
        if split_at < 1:
            split_at = limit
        cut = split_at + 1 if remaining[split_at] == "\n" else split_at
        chunks.append(remaining[:cut])
        remaining = remaining[cut:]
    if remaining:
        chunks.append(remaining)
    return chunks


def _rendered_telegram_chunks(text: str) -> list[tuple[str, str]]:
    """Return source/rendered chunks that both fit Telegram's HTML limit."""

    rendered_chunks: list[tuple[str, str]] = []
    for initial in split_telegram_text(text):
        remaining = initial
        while remaining:
            rendered = telegram_markdown_to_html(remaining)
            if len(rendered) <= TELEGRAM_TEXT_LIMIT:
                rendered_chunks.append((remaining, rendered))
                break

            low, high = 1, min(len(remaining), TELEGRAM_TEXT_LIMIT)
            best = 1
            while low <= high:
                middle = (low + high) // 2
                if len(telegram_markdown_to_html(remaining[:middle])) <= TELEGRAM_TEXT_LIMIT:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            newline = remaining[:best].rfind("\n")
            cut = newline + 1 if newline >= max(1, best // 3) else best
            source = remaining[:cut]
            rendered_chunks.append((source, telegram_markdown_to_html(source)))
            remaining = remaining[cut:]
    return rendered_chunks


def telegram_api_post(token: str, method: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Call the Telegram Bot API without logging the bot token."""

    url = f"https://api.telegram.org/bot{token}/{method}"
    request = urllib.request.Request(
        url,
        data=json.dumps(dict(payload)).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"telegram_http_error_{exc.code}") from None
    except urllib.error.URLError:
        raise RuntimeError("telegram_unreachable") from None
    if not body.get("ok"):
        raise RuntimeError("telegram_api_error")
    return body


def make_telegram_sender(
    *,
    token: str,
    default_chat_id: str | None,
    default_thread_id: str | None,
    poster: Callable[..., dict[str, Any]] = telegram_api_post,
):
    """Build a sender that targets a configured Telegram chat or topic."""

    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is required for live delivery.")

    def sender(
        *,
        text: str,
        chat_id: str | None = None,
        thread_id: str | None = None,
        backend: str | None = None,
        attachments: list[str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str | list[str | int]:
        _ = (backend, attachments, payload)
        target_chat = chat_id or default_chat_id
        if not target_chat:
            raise RuntimeError(
                "No Telegram chat id configured. Set MEETING_ASSISTANT_TELEGRAM_CHAT_ID."
            )
        thread = _coerce_thread_id(thread_id if thread_id is not None else default_thread_id)
        message_ids: list[str | int] = []
        for _, rendered_chunk in _rendered_telegram_chunks(text):
            body: dict[str, Any] = {
                "chat_id": target_chat,
                "text": rendered_chunk,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if thread is not None:
                body["message_thread_id"] = thread
            response = poster(token=token, method="sendMessage", payload=body)
            message_ids.append(str(response.get("result", {}).get("message_id", "")))
        return str(message_ids[0]) if len(message_ids) == 1 else message_ids

    return sender


def iter_pending(root: str | Path) -> list[OutboxPayload]:
    """Return queued or retryable outbox payloads under ``root``."""

    pending: list[OutboxPayload] = []
    for path in discover_outbox_files(root):
        try:
            payload = OutboxPayload.from_file(path)
        except (ValueError, json.JSONDecodeError):
            continue
        if payload.status in PENDING_STATUSES:
            pending.append(payload)
    return pending


def _summary_line(payload: OutboxPayload, *, action: str, detail: str = "") -> str:
    name = Path(payload.path).name
    line = f"{action} {payload.meeting_id} {payload.kind} ({name})"
    return f"{line} {detail}".rstrip()


def run(
    *,
    storage_root: str | Path,
    backend: str,
    chat_id: str | None,
    thread_id: str | None,
    token: str,
    dry_run: bool = False,
    poster: Callable[..., dict[str, Any]] = telegram_api_post,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Deliver all pending outbox payloads in one pass."""

    pending = iter_pending(storage_root)
    if not pending:
        return 0
    if dry_run:
        for payload in pending:
            target = payload.chat_id or chat_id or "(not configured)"
            thread = payload.thread_id or thread_id or "-"
            print(
                _summary_line(
                    payload,
                    action="DRY-RUN would send",
                    detail=f"-> {target}/{thread}",
                ),
                file=out,
            )
        return 0

    sender = make_telegram_sender(
        token=token,
        default_chat_id=chat_id,
        default_thread_id=thread_id,
        poster=poster,
    )
    delivered: list[str] = []
    failed: list[str] = []
    root_path = Path(storage_root)
    store: MeetingStore | None = None
    if root_path.is_dir():
        try:
            store = MeetingStore(root_path)
        except Exception:  # noqa: BLE001 - delivery must still be attempted
            store = None
    try:
        for payload in pending:
            outcome = deliver_outbox_file(payload.path, sender=sender, store=store)
            if outcome.status == DELIVERED_STATUS:
                ids = ",".join(outcome.message_ids) or "?"
                delivered.append(_summary_line(payload, action="delivered", detail=f"msg={ids}"))
            else:
                failed.append(
                    _summary_line(payload, action="FAILED", detail=str(outcome.error or ""))
                )
    finally:
        if store is not None:
            store.close()

    _ = backend
    for line in delivered:
        print(line, file=out)
    for line in failed:
        print(line, file=err)
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deliver meeting-assistant outbox messages to Telegram."
    )
    parser.add_argument("--storage-root", default=None, help="storage root or a single outbox JSON")
    parser.add_argument("--target", default=None, help="delivery backend label")
    parser.add_argument("--chat-id", default=None, help="override Telegram chat id")
    parser.add_argument("--thread-id", default=None, help="override Telegram topic id")
    parser.add_argument("--dry-run", action="store_true", help="show pending sends without sending")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    load_env_file()
    storage_root = (
        args.storage_root
        or os.environ.get("MEETING_ASSISTANT_STORAGE_DIR")
        or DEFAULT_STORAGE_DIR
    )
    backend = args.target or os.environ.get("MEETING_ASSISTANT_TELEGRAM_BACKEND", DEFAULT_BACKEND)
    chat_id = args.chat_id or os.environ.get("MEETING_ASSISTANT_TELEGRAM_CHAT_ID")
    thread_id = args.thread_id or os.environ.get("MEETING_ASSISTANT_TELEGRAM_THREAD_ID")
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not Path(storage_root).exists():
        return 0
    return run(
        storage_root=storage_root,
        backend=backend,
        chat_id=chat_id,
        thread_id=thread_id,
        token=token,
        dry_run=args.dry_run,
    )


__all__ = [
    "DEFAULT_STORAGE_DIR",
    "DEFAULT_BACKEND",
    "DELIVERED_STATUS",
    "FAILED_STATUS",
    "QUEUED_STATUS",
    "OutboxPayload",
    "iter_pending",
    "make_telegram_sender",
    "split_telegram_text",
    "telegram_api_post",
    "telegram_markdown_to_html",
    "run",
    "build_parser",
    "main",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
