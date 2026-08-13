"""Recall-native summary extraction, persistence, and Telegram rendering.

MVP note source: use Recall.ai's native summary/analysis artifact when it is
present. This module is deliberately tolerant about provider payload shape while
remaining conservative: if no native summary is found, it returns ``None`` and
queues nothing rather than fabricating notes with another model.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Mapping

from recall_meeting_assistant.email_delivery import summary_markdown_to_email_html
from recall_meeting_assistant.outbox import (
    queue_summary_email_notification,
    queue_summary_notification,
)
from recall_meeting_assistant.participants import ParticipantDirectory, format_action_owner
from recall_meeting_assistant.storage import MeetingNotes, MeetingStore
from recall_meeting_assistant.summary_recipients import summary_recipient_emails_from_bot

RECALL_SUMMARY_PROVIDER = "recall_ai"
RECALL_SUMMARY_MODEL = "recall_native_summary"
RECALL_SUMMARY_JSON = "recall_summary.json"
SUMMARY_MARKDOWN = "summary.md"
OUTBOX_SUMMARY_KEY = "outbox_summary_json"
OUTBOX_SUMMARY_EMAIL_KEY = "outbox_summary_email_json"
SUMMARY_EMAIL_RECIPIENTS_JSON = "summary_email_recipients.json"

_SUMMARY_KEYS = (
    "summary",
    "ai_summary",
    "analysis",
    "meeting_summary",
    "recap",
    "notes",
)
_EXECUTIVE_KEYS = (
    "executive_summary",
    "key_points",
    "bullets",
    "points",
    "highlights",
)
_SHORT_KEYS = ("short_summary", "summary_text", "text", "description", "overview")
_ACTION_KEYS = ("action_items", "actions", "action_points", "todos", "tasks")
_DECISION_KEYS = ("decisions", "decision_items")
_QUESTION_KEYS = ("open_questions", "questions", "parking_lot")
_RISK_KEYS = ("risks", "blockers")
_TITLE_KEYS = ("title", "meeting_title", "name")


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return [value]


def _strings(value: Any) -> list[str]:
    out: list[str] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            text = _clean_str(
                item.get("text") or item.get("summary") or item.get("point") or item.get("title")
            )
        else:
            text = _clean_str(item)
        if text:
            out.append(text)
    return out


def _dict_items(value: Any, *, default_key: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in _as_list(value):
        if isinstance(item, Mapping):
            cleaned = {str(k): v for k, v in item.items() if v is not None and str(v).strip()}
            if cleaned:
                out.append(cleaned)
        else:
            text = _clean_str(item)
            if text:
                out.append({default_key: text})
    return out


def _first_present(payload: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in payload and payload[key] not in (None, "", [], {}):
            return payload[key]
    return None


def _candidate_containers(source: Any) -> list[Mapping[str, Any]]:
    if not isinstance(source, Mapping):
        return []
    candidates: list[Mapping[str, Any]] = []
    for key in _SUMMARY_KEYS:
        value = source.get(key)
        if isinstance(value, Mapping):
            candidates.append(value)
        elif isinstance(value, str) and value.strip():
            candidates.append({"short_summary": value})
    data = source.get("data")
    if isinstance(data, Mapping):
        candidates.extend(_candidate_containers(data))
    # Some APIs put the summary fields directly on the resource.
    if any(key in source for key in (*_EXECUTIVE_KEYS, *_SHORT_KEYS, *_ACTION_KEYS)):
        candidates.append(source)
    return candidates


@dataclass(frozen=True)
class RecallNativeSummary:
    """A normalized Recall-native summary artifact."""

    title: str | None = None
    short_summary: str | None = None
    executive_summary: list[str] = field(default_factory=list)
    action_items: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    open_questions: list[dict[str, Any]] = field(default_factory=list)
    risks: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_empty(self) -> bool:
        return not any(
            [
                self.short_summary,
                self.executive_summary,
                self.action_items,
                self.decisions,
                self.open_questions,
                self.risks,
            ]
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "short_summary": self.short_summary,
            "executive_summary": list(self.executive_summary),
            "action_items": list(self.action_items),
            "decisions": list(self.decisions),
            "open_questions": list(self.open_questions),
            "risks": list(self.risks),
            "provider": RECALL_SUMMARY_PROVIDER,
            "model": RECALL_SUMMARY_MODEL,
        }

    def to_meeting_notes(self, meeting_id: str) -> MeetingNotes:
        executive = list(self.executive_summary)
        if not executive and self.short_summary:
            executive = [self.short_summary]
        return MeetingNotes(
            meeting_id=meeting_id,
            executive_summary=executive,
            executive_summary_markdown=render_summary_markdown(self, title=self.title),
            action_items=list(self.action_items),
            decisions=list(self.decisions),
            open_questions=list(self.open_questions),
            risks=list(self.risks),
            model_provider=RECALL_SUMMARY_PROVIDER,
            model_name=RECALL_SUMMARY_MODEL,
        )


def _normalize_summary(container: Mapping[str, Any]) -> RecallNativeSummary | None:
    title = _clean_str(_first_present(container, _TITLE_KEYS))
    short_summary = _clean_str(_first_present(container, _SHORT_KEYS))
    executive = _strings(_first_present(container, _EXECUTIVE_KEYS))
    action_items = _dict_items(_first_present(container, _ACTION_KEYS), default_key="task")
    decisions = _dict_items(_first_present(container, _DECISION_KEYS), default_key="text")
    open_questions = _dict_items(_first_present(container, _QUESTION_KEYS), default_key="question")
    risks = _dict_items(_first_present(container, _RISK_KEYS), default_key="text")
    summary = RecallNativeSummary(
        title=title,
        short_summary=short_summary,
        executive_summary=executive,
        action_items=action_items,
        decisions=decisions,
        open_questions=open_questions,
        risks=risks,
        raw=dict(container),
    )
    return None if summary.is_empty else summary


def extract_recall_summary(*sources: Mapping[str, Any] | None) -> RecallNativeSummary | None:
    """Find and normalize the first non-empty Recall-native summary in sources."""

    for source in sources:
        if not source:
            continue
        for candidate in _candidate_containers(source):
            summary = _normalize_summary(candidate)
            if summary is not None:
                return summary
    return None


def _line_for_item(item: Mapping[str, Any], *, key: str) -> str | None:
    text = _clean_str(item.get(key) or item.get("text") or item.get("title") or item.get("task"))
    return text


def render_summary_markdown(summary: RecallNativeSummary, *, title: str | None = None) -> str:
    """Render a storage-friendly Markdown summary."""

    heading = title or summary.title or "Meeting summary"
    lines = [f"# {heading}", "", f"_Source: {RECALL_SUMMARY_PROVIDER}_", ""]
    if summary.short_summary:
        lines.extend([summary.short_summary, ""])
    points = summary.executive_summary
    if points:
        lines.extend(["## Summary", *[f"- {p}" for p in points], ""])
    if summary.decisions:
        lines.extend(["## Decisions"])
        for item in summary.decisions:
            text = _line_for_item(item, key="decision")
            if text:
                lines.append(f"- {text}")
        lines.append("")
    if summary.action_items:
        lines.extend(["## Action items"])
        for item in summary.action_items:
            task = _clean_str(item.get("task") or item.get("text") or item.get("title")) or "Action item"
            owner = _clean_str(item.get("owner") or item.get("assignee")) or "Unassigned"
            due = _clean_str(item.get("due_date") or item.get("due"))
            suffix = f" by {due}" if due else ""
            lines.append(f"- {owner}: {task}{suffix}")
        lines.append("")
    if summary.open_questions:
        lines.extend(["## Open questions"])
        for item in summary.open_questions:
            text = _line_for_item(item, key="question")
            if text:
                lines.append(f"- {text}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_summary_message(
    notes: MeetingNotes,
    *,
    title: str | None = None,
    directory: ParticipantDirectory | None = None,
) -> str:
    """Render concise Telegram-ready summary text from stored notes."""

    heading = title or "Meeting summary"
    lines = [f"**{heading}**", "", "Source: Recall.ai native summary"]
    if notes.executive_summary:
        lines.extend(["", "**Summary**"])
        lines.extend(f"• {item}" for item in notes.executive_summary)
    if notes.decisions:
        lines.extend(["", "**Decisions**"])
        for item in notes.decisions:
            text = _line_for_item(item, key="decision")
            if text:
                lines.append(f"• {text}")
    if notes.action_items:
        lines.extend(["", "**Action items**"])
        for item in notes.action_items:
            task = _clean_str(item.get("task") or item.get("text") or item.get("title")) or "Action item"
            owner_raw = _clean_str(item.get("owner") or item.get("assignee"))
            owner = (
                format_action_owner(directory, owner_raw)
                if directory is not None
                else (owner_raw or "Unassigned")
            )
            due = _clean_str(item.get("due_date") or item.get("due"))
            due_part = f" · due {due}" if due else ""
            lines.append(f"• {owner}: {task}{due_part}")
    if notes.open_questions:
        lines.extend(["", "**Open questions**"])
        for item in notes.open_questions:
            text = _line_for_item(item, key="question")
            if text:
                lines.append(f"• {text}")
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class RecallSummaryIngestResult:
    available: bool
    meeting_id: str
    artifact_paths: dict[str, str] = field(default_factory=dict)
    reason: str = "summary_unavailable"


def _write_json(store: MeetingStore, meeting_id: str, filename: str, payload: Any) -> str:
    path = store.artifact_path(meeting_id, filename, create_parents=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")
    return str(path)


def _write_text(store: MeetingStore, meeting_id: str, filename: str, text: str) -> str:
    path = store.artifact_path(meeting_id, filename, create_parents=True)
    path.write_text(text)
    return str(path)


def ingest_recall_summary(
    store: MeetingStore,
    *,
    meeting_id: str,
    resource: Mapping[str, Any] | None = None,
    bot_payload: Mapping[str, Any] | None = None,
    directory: ParticipantDirectory | None = None,
    chat_id: str | None = None,
    thread_id: str | None = None,
    backend: str = "telegram",
    summary_recipient_emails: Iterable[str] | None = None,
) -> RecallSummaryIngestResult:
    """Persist Recall-native summary artifacts and queue a summary outbox message.

    If Recall does not provide a summary/analysis payload, the function is a safe
    no-op: no notes, no fake summary, and no delivery outboxes.
    """

    summary = extract_recall_summary(resource, bot_payload)
    if summary is None:
        return RecallSummaryIngestResult(False, meeting_id, reason="summary_unavailable")

    notes = summary.to_meeting_notes(meeting_id)
    store.save_notes(notes)
    markdown = render_summary_markdown(summary, title=summary.title)
    message = build_summary_message(notes, title=summary.title, directory=directory)
    artifact_paths: dict[str, str] = {
        "recall_summary_json": _write_json(store, meeting_id, RECALL_SUMMARY_JSON, summary.to_dict()),
        "summary_markdown": _write_text(store, meeting_id, SUMMARY_MARKDOWN, markdown),
    }
    artifact_paths[OUTBOX_SUMMARY_KEY] = queue_summary_notification(
        store,
        meeting_id=meeting_id,
        text=message,
        backend=backend,
        chat_id=chat_id,
        thread_id=thread_id,
    )
    email_recipients = summary_recipient_emails_from_bot(
        bot_payload,
        explicit=summary_recipient_emails,
    )
    if email_recipients:
        artifact_paths["summary_email_recipients_json"] = _write_json(
            store,
            meeting_id,
            SUMMARY_EMAIL_RECIPIENTS_JSON,
            {"recipients": email_recipients},
        )
        artifact_paths[OUTBOX_SUMMARY_EMAIL_KEY] = queue_summary_email_notification(
            store,
            meeting_id=meeting_id,
            text=message,
            recipients=email_recipients,
            subject=f"Meeting summary: {summary.title or meeting_id}",
            html_body=summary_markdown_to_email_html(message),
        )
    return RecallSummaryIngestResult(True, meeting_id, artifact_paths, reason="summary_ingested")


__all__ = [
    "OUTBOX_SUMMARY_KEY",
    "OUTBOX_SUMMARY_EMAIL_KEY",
    "RECALL_SUMMARY_JSON",
    "RECALL_SUMMARY_MODEL",
    "RECALL_SUMMARY_PROVIDER",
    "SUMMARY_MARKDOWN",
    "SUMMARY_EMAIL_RECIPIENTS_JSON",
    "RecallNativeSummary",
    "RecallSummaryIngestResult",
    "build_summary_message",
    "extract_recall_summary",
    "ingest_recall_summary",
    "render_summary_markdown",
]
