"""Exact allowlist and participant-aware recipient selection for summaries."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from email.utils import parseaddr
from typing import Any

SUMMARY_EMAIL_ALLOWLIST = frozenset(
    {
        "gauthier@zyf.ai",
        "clement.fel@gmail.com",
        "ihebabidi007@gmail.com",
        "minjifromsk@gmail.com",
        "paul@zyf.ai",
        "utkir@zyf.ai",
        "neutize@ondefy.com",
        "utkir@zyfi.org",
        "gabin.furon2106@gmail.com",
    }
)


def normalize_email(value: Any) -> str:
    """Normalize a header-style or plain email value."""

    _, address = parseaddr(str(value or ""))
    return address.strip().lower()


def _iter_email_values(value: Any) -> Iterable[str]:
    if isinstance(value, Mapping):
        found = False
        for key in ("email", "email_address", "address", "mail"):
            if value.get(key):
                found = True
                yield str(value[key])
        if found:
            return
        for key in ("attendees", "participants", "organizer", "creator"):
            if key in value:
                yield from _iter_email_values(value[key])
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _iter_email_values(item)
        return
    if value is not None:
        yield str(value)


def allowlisted_summary_recipients(values: Any) -> list[str]:
    """Return unique recipients that match the exact nine-address allowlist."""

    recipients: list[str] = []
    seen: set[str] = set()
    for value in _iter_email_values(values):
        address = normalize_email(value)
        if address in SUMMARY_EMAIL_ALLOWLIST and address not in seen:
            seen.add(address)
            recipients.append(address)
    return recipients


def summary_recipients_from_event(event: Mapping[str, Any] | None) -> list[str]:
    """Select allowlisted people from a Calendar or parsed iCalendar event."""

    if not isinstance(event, Mapping):
        return []
    candidates: list[Any] = [event.get("attendees", [])]
    candidates.extend(event.get(key) for key in ("organizer", "creator"))
    private = ((event.get("extendedProperties") or {}).get("private") or {})
    if private.get("summary_recipient_emails"):
        candidates.append(str(private["summary_recipient_emails"]).split(","))
    return allowlisted_summary_recipients(candidates)


def _actual_participant_recipients(bot_payload: Mapping[str, Any]) -> tuple[bool, list[str]]:
    """Return whether Recall exposed participant emails and their allowlisted subset."""

    for key in ("meeting_participants", "participants"):
        if key not in bot_payload:
            continue
        values = list(_iter_email_values(bot_payload.get(key)))
        if values:
            return True, allowlisted_summary_recipients(values)
    return False, []


def _metadata_recipient_values(value: Any) -> Any:
    """Decode JSON-serialized Recall metadata while keeping string fallback safe."""
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    return decoded if isinstance(decoded, (list, tuple, set, frozenset, Mapping)) else value


def summary_recipient_emails_from_bot(
    bot_payload: Mapping[str, Any] | None,
    *,
    explicit: Iterable[str] | None = None,
) -> list[str]:
    """Prefer actual Recall participants, then fall back to invite metadata.

    The fallback is used for new bots whose Recall response does not expose
    participant email data. It is still restricted to the exact allowlist.
    """

    payload = bot_payload if isinstance(bot_payload, Mapping) else {}
    has_actual, actual = _actual_participant_recipients(payload)
    if has_actual:
        return actual

    metadata = payload.get("metadata")
    if isinstance(metadata, Mapping) and metadata.get("summary_recipient_emails"):
        return allowlisted_summary_recipients(
            _metadata_recipient_values(metadata["summary_recipient_emails"])
        )

    if payload.get("summary_recipient_emails"):
        return allowlisted_summary_recipients(payload["summary_recipient_emails"])
    return allowlisted_summary_recipients(list(explicit or []))


__all__ = [
    "SUMMARY_EMAIL_ALLOWLIST",
    "allowlisted_summary_recipients",
    "normalize_email",
    "summary_recipient_emails_from_bot",
    "summary_recipients_from_event",
]