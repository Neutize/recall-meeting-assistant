"""Environment-configured participant selection for summary email delivery."""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from email.utils import parseaddr
from typing import Any

SUMMARY_EMAIL_ALLOWLIST_ENV = "MEETING_ASSISTANT_SUMMARY_EMAIL_ALLOWLIST"


def normalize_email(value: Any) -> str:
    """Normalize a header-style or plain email value."""

    _, address = parseaddr(str(value or ""))
    return address.strip().lower()


def get_summary_email_allowlist(
    env: Mapping[str, str] | None = None,
) -> frozenset[str]:
    """Return the configured exact-address allowlist.

    The environment value is read for every selection so callers and tests can
    update configuration without reloading the module. Missing, blank, or
    comma-only values intentionally disable summary email delivery.
    """

    source = os.environ if env is None else env
    raw = source.get(SUMMARY_EMAIL_ALLOWLIST_ENV, "")
    return frozenset(
        address
        for item in raw.split(",")
        if (address := normalize_email(item)) and "@" in address
    )


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
    """Return unique recipients that match the configured exact allowlist."""

    allowlist = get_summary_email_allowlist()
    if not allowlist:
        return []

    recipients: list[str] = []
    seen: set[str] = set()
    for value in _iter_email_values(values):
        address = normalize_email(value)
        if address in allowlist and address not in seen:
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
    participant email data. It remains restricted to the environment-configured
    exact-address allowlist.
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
    "SUMMARY_EMAIL_ALLOWLIST_ENV",
    "allowlisted_summary_recipients",
    "get_summary_email_allowlist",
    "normalize_email",
    "summary_recipient_emails_from_bot",
    "summary_recipients_from_event",
]
