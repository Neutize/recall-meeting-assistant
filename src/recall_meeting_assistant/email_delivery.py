"""Gmail delivery adapter for allowlisted meeting summaries."""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import sys
from email.message import EmailMessage
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
from recall_meeting_assistant.summary_recipients import (
    allowlisted_summary_recipients,
    normalize_email,
)

DEFAULT_STORAGE_DIR = str(get_product_home() / "meetings")


def _default_token_path() -> Path:
    explicit = os.environ.get("GOOGLE_TOKEN_PATH")
    if explicit:
        return Path(explicit).expanduser()
    candidates = [
        Path(os.environ.get("HERMES_HOME", "/opt/data")) / "google_token.json",
        Path("/opt/data/google_token.json"),
    ]
    return next((candidate for candidate in candidates if candidate.is_file()), candidates[-1])


DEFAULT_TOKEN_PATH = _default_token_path()
GMAIL_BACKEND = "gmail"
PENDING_STATUSES = {QUEUED_STATUS, FAILED_STATUS}
GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.send",
]
_BOLD_MARKDOWN_RE = re.compile(r"\*\*([^*\n][\s\S]*?[^*\n])\*\*")


def summary_markdown_to_email_html(text: str) -> str:
    """Render the small summary Markdown subset as safe email HTML."""

    escaped = html.escape(text, quote=False)
    escaped = _BOLD_MARKDOWN_RE.sub(r"<strong>\1</strong>", escaped)
    lines = escaped.splitlines() or [""]
    rendered = "<br>\n".join(lines)
    return f'<div style="font-family:Arial,sans-serif;line-height:1.5">{rendered}</div>'


def build_email_message(
    *,
    recipients: list[str],
    subject: str,
    text: str,
    html_body: str | None = None,
) -> EmailMessage:
    """Build a multipart plain-text and HTML message without external side effects."""

    message = EmailMessage()
    message["To"] = ", ".join(recipients)
    message["Subject"] = subject.replace("\r", " ").replace("\n", " ").strip()[:200]
    message["Auto-Submitted"] = "auto-generated"
    message["X-Auto-Response-Suppress"] = "All"
    message.set_content(text)
    message.add_alternative(html_body or summary_markdown_to_email_html(text), subtype="html")
    return message


def _validate_send_only_token(path: Path) -> None:
    """Reject tokens whose stored grant is broader than Gmail send-only use."""

    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("google_token_invalid") from exc
    if not isinstance(info, Mapping):
        raise RuntimeError("google_token_invalid")

    raw_scopes = info.get("scopes")
    if isinstance(raw_scopes, str):
        stored_scopes = {scope for scope in raw_scopes.split() if scope}
    elif isinstance(raw_scopes, (list, tuple, set, frozenset)):
        stored_scopes = {str(scope).strip() for scope in raw_scopes if str(scope).strip()}
    else:
        stored_scopes = set()

    allowed_scopes = set(GMAIL_SCOPES)
    if stored_scopes - allowed_scopes:
        raise RuntimeError("gmail_token_broader_scopes_require_reauth")
    if stored_scopes != allowed_scopes:
        raise RuntimeError("gmail_token_scopes_require_reauth")


def load_gmail_credentials(token_path: str | Path):
    """Load and refresh the existing Gmail OAuth token in memory only."""

    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = Path(token_path)
    if not path.is_file():
        raise RuntimeError(f"missing_google_token:{path}")
    _validate_send_only_token(path)
    credentials = Credentials.from_authorized_user_file(str(path), scopes=GMAIL_SCOPES)
    if credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())
    if not credentials.valid:
        raise RuntimeError("google_token_invalid")
    return credentials


def build_gmail_service(token_path: str | Path):
    """Build the Gmail API service lazily so offline tests need no Google imports."""

    from googleapiclient.discovery import build

    credentials = load_gmail_credentials(token_path)
    return build("gmail", "v1", credentials=credentials, cache_discovery=False)


def make_gmail_sender(service: Any) -> Callable[..., str]:
    """Build a sender that enforces the exact summary recipient allowlist."""

    def sender(
        *,
        text: str,
        chat_id: str | None = None,
        thread_id: str | None = None,
        backend: str | None = None,
        attachments: list[str] | None = None,
        payload: Mapping[str, Any] | None = None,
    ) -> str:
        _ = (chat_id, thread_id, backend, attachments)
        data = payload or {}
        raw_recipients = data.get("recipients") or []
        recipients = [normalize_email(item) for item in raw_recipients]
        if not recipients or any(not recipient for recipient in recipients):
            raise RuntimeError("summary_email_recipients_missing")
        if len(set(recipients)) != len(recipients):
            raise RuntimeError("summary_email_recipients_duplicate")
        if allowlisted_summary_recipients(recipients) != recipients:
            raise RuntimeError("summary_email_recipient_not_allowlisted")

        subject = str(data.get("subject") or "Meeting summary")
        html_body = str(data.get("html_body") or summary_markdown_to_email_html(text))
        message = build_email_message(
            recipients=recipients,
            subject=subject,
            text=text,
            html_body=html_body,
        )
        encoded = base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")
        result = service.users().messages().send(
            userId="me",
            body={"raw": encoded},
        ).execute()
        message_id = str(result.get("id") or result.get("threadId") or "").strip()
        if not message_id:
            raise RuntimeError("gmail_send_missing_message_id")
        return message_id

    return sender


def iter_pending(root: str | Path) -> list[OutboxPayload]:
    """Return only queued Gmail summary outboxes."""

    pending: list[OutboxPayload] = []
    for path in discover_outbox_files(root):
        try:
            payload = OutboxPayload.from_file(path)
        except (ValueError, json.JSONDecodeError):
            continue
        if (
            payload.status in PENDING_STATUSES
            and payload.backend == GMAIL_BACKEND
            and payload.kind == "meeting_summary"
        ):
            pending.append(payload)
    return pending


def _summary_line(payload: OutboxPayload, *, action: str, detail: str = "") -> str:
    line = f"{action} {payload.meeting_id} {payload.kind} ({Path(payload.path).name})"
    return f"{line} {detail}".rstrip()


def run(
    *,
    storage_root: str | Path,
    token_path: str | Path = DEFAULT_TOKEN_PATH,
    dry_run: bool = False,
    service: Any | None = None,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    """Deliver each pending summary email once, retaining retryable state."""

    pending = iter_pending(storage_root)
    if not pending:
        return 0
    if dry_run:
        for payload in pending:
            recipients = ",".join(payload.recipients) or "(missing recipients)"
            print(_summary_line(payload, action="DRY-RUN would email", detail=f"-> {recipients}"), file=out)
        return 0

    sender = make_gmail_sender(service or build_gmail_service(token_path))
    delivered: list[str] = []
    failed: list[str] = []
    for payload in pending:
        outcome = deliver_outbox_file(payload.path, sender=sender, store=None)
        if outcome.status == DELIVERED_STATUS:
            ids = ",".join(outcome.message_ids) or "?"
            delivered.append(_summary_line(payload, action="delivered", detail=f"msg={ids}"))
        else:
            failed.append(_summary_line(payload, action="FAILED", detail="delivery_failed"))

    for line in delivered:
        print(line, file=out)
    for line in failed:
        print(line, file=err)
    return 0 if not failed else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Deliver allowlisted meeting summaries by Gmail")
    parser.add_argument("--storage-root", default=None, help="meeting storage root")
    parser.add_argument("--token-path", default=None, help="Google OAuth token JSON path")
    parser.add_argument("--dry-run", action="store_true", help="show pending emails without sending")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    load_env_file()
    storage_root = args.storage_root or os.environ.get("MEETING_ASSISTANT_STORAGE_DIR") or DEFAULT_STORAGE_DIR
    token_path = args.token_path or os.environ.get("GOOGLE_TOKEN_PATH") or str(_default_token_path())
    if not Path(storage_root).exists():
        return 0
    try:
        return run(
            storage_root=storage_root,
            token_path=token_path,
            dry_run=args.dry_run,
        )
    except Exception as exc:  # noqa: BLE001 - keep cron output secret-safe
        print(f"Meeting summary email watcher error: {type(exc).__name__}", file=sys.stderr)
        return 1


__all__ = [
    "DEFAULT_STORAGE_DIR",
    "DEFAULT_TOKEN_PATH",
    "GMAIL_BACKEND",
    "GMAIL_SCOPES",
    "build_email_message",
    "build_gmail_service",
    "iter_pending",
    "load_gmail_credentials",
    "make_gmail_sender",
    "main",
    "run",
    "summary_markdown_to_email_html",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
