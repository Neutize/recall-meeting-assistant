"""Conservative participant mapping for Telegram delivery.

Phase 3 of docs/architecture.md, scoped down to
the MVP's hard requirement: map Google Meet display-name *aliases* / emails to
Telegram handles using **exact, conservative** matching only.  This module never
fuzzy-matches and never tags an ambiguous person, so the delivery layer can
mention owners without risking a wrong @mention.

Two rules hold here:

1. **Exact only.**  Email matches are normalized (trimmed + casefolded) and must
   be equal; alias matches normalize whitespace + case but require equality.
   There is no substring / fuzzy / nickname matching.
2. **Ambiguity is never tagged.**  If an alias resolves to more than one person
   the match is :data:`REASON_AMBIGUOUS` and not taggable.  Callers fall back to
   a plain name or ``Unassigned`` rather than guessing.

The mapping config is YAML or JSON (see the plan's ``participants.yaml`` shape).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from recall_meeting_assistant.models import MeetingParticipant

# Match reason codes (stored on MeetingParticipant.match_reason).
MATCH_REASON_EMAIL = "exact_email"
MATCH_REASON_ALIAS = "exact_alias"
REASON_UNMATCHED = "unmatched"
REASON_AMBIGUOUS = "ambiguous"
REASON_NO_INPUT = "no_input"

#: Confidence assigned to each successful match kind.  Email is the strongest
#: signal; an exact alias is slightly lower but still confident.
EMAIL_CONFIDENCE = 1.0
ALIAS_CONFIDENCE = 0.9

DEFAULT_OWNER_FALLBACK = "Unassigned"
#: Owner strings that should always render as the unassigned fallback.
_UNASSIGNED_TOKENS = frozenset({"", "unassigned", "unknown", "n/a", "tbd", "none"})


# ── normalization helpers ────────────────────────────────────────────────────
def _norm_alias(value: Any) -> str:
    """Collapse internal whitespace and casefold for exact alias comparison."""
    return " ".join(str(value).split()).casefold()


def _norm_email(value: Any) -> str:
    return str(value).strip().casefold()


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _clean_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


# ── person + match records ───────────────────────────────────────────────────
@dataclass(frozen=True)
class Person:
    """One mapped team member from the participant config."""

    canonical_name: str
    emails: tuple[str, ...] = ()
    meet_aliases: tuple[str, ...] = ()
    telegram_username: str | None = None
    telegram_user_id: str | None = None
    tags: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "Person":
        canonical = _clean_str(payload.get("canonical_name"))
        if not canonical:
            raise ValueError("Each participant entry needs a canonical_name.")
        telegram = payload.get("telegram")
        if isinstance(telegram, Mapping):
            username = telegram.get("username")
            user_id = telegram.get("user_id")
        else:
            username = payload.get("telegram_username")
            user_id = payload.get("telegram_user_id")
        username_clean = _clean_str(username)
        return cls(
            canonical_name=canonical,
            emails=tuple(e for e in (_clean_str(x) for x in _as_list(payload.get("emails"))) if e),
            meet_aliases=tuple(
                a for a in (_clean_str(x) for x in _as_list(payload.get("meet_aliases"))) if a
            ),
            telegram_username=username_clean.lstrip("@") if username_clean else None,
            telegram_user_id=_clean_str(user_id),
            tags=tuple(t for t in (_clean_str(x) for x in _as_list(payload.get("tags"))) if t),
        )


@dataclass(frozen=True)
class ParticipantMatch:
    """The verdict of matching one display name / email against the directory."""

    matched: bool
    reason: str
    confidence: float = 0.0
    canonical_name: str | None = None
    telegram_username: str | None = None
    telegram_user_id: str | None = None

    @property
    def is_taggable(self) -> bool:
        """True only when a confident match also has a Telegram handle to tag."""
        return self.matched and bool(self.telegram_username or self.telegram_user_id)


_UNMATCHED = ParticipantMatch(matched=False, reason=REASON_UNMATCHED)
_NO_INPUT = ParticipantMatch(matched=False, reason=REASON_NO_INPUT)
_AMBIGUOUS = ParticipantMatch(matched=False, reason=REASON_AMBIGUOUS)


def _match_from_person(person: Person, *, reason: str, confidence: float) -> ParticipantMatch:
    return ParticipantMatch(
        matched=True,
        reason=reason,
        confidence=confidence,
        canonical_name=person.canonical_name,
        telegram_username=person.telegram_username,
        telegram_user_id=person.telegram_user_id,
    )


# ── directory ────────────────────────────────────────────────────────────────
class ParticipantDirectory:
    """Exact-match index over a set of :class:`Person` records.

    Build via :meth:`from_file` / :meth:`from_mapping`.  An alias (or email) that
    maps to more than one person is recorded as ambiguous and never tags anyone.
    """

    def __init__(self, people: list[Person]) -> None:
        self._people = list(people)
        self._by_email: dict[str, list[Person]] = {}
        self._by_alias: dict[str, list[Person]] = {}
        for person in self._people:
            for email in person.emails:
                self._index(self._by_email, _norm_email(email), person)
            # The canonical name is an implicit alias alongside explicit ones.
            for alias in (person.canonical_name, *person.meet_aliases):
                self._index(self._by_alias, _norm_alias(alias), person)

    @staticmethod
    def _index(table: dict[str, list[Person]], key: str, person: Person) -> None:
        if not key:
            return
        bucket = table.setdefault(key, [])
        if person not in bucket:
            bucket.append(person)

    # ── construction ─────────────────────────────────────────────────────────
    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | list[Any] | None) -> "ParticipantDirectory":
        """Build a directory from a parsed mapping config (``{"people": [...]}``).

        A bare list of person dicts is also accepted.
        """
        if data is None:
            entries: list[Any] = []
        elif isinstance(data, Mapping):
            entries = list(data.get("people") or [])
        elif isinstance(data, list):
            entries = data
        else:
            raise ValueError("Participant config must be a mapping or list.")
        people = [Person.from_dict(entry) for entry in entries if isinstance(entry, Mapping)]
        return cls(people)

    @classmethod
    def from_file(cls, path: str | Path, *, missing_ok: bool = False) -> "ParticipantDirectory":
        """Load a directory from a YAML or JSON participant-map file.

        ``missing_ok`` returns an empty directory instead of raising when the
        file does not exist (handy for environments without a configured map).
        """
        file_path = Path(path)
        if not file_path.exists():
            if missing_ok:
                return cls([])
            raise FileNotFoundError(f"Participant map not found: {file_path}")
        text = file_path.read_text()
        suffix = file_path.suffix.lower()
        if suffix == ".json":
            data = json.loads(text or "{}")
        else:
            import yaml  # PyYAML parses JSON too, so it is a safe default.

            data = yaml.safe_load(text)
        return cls.from_mapping(data)

    @classmethod
    def from_config(cls, config: Any, *, missing_ok: bool = True) -> "ParticipantDirectory":
        """Build from a :class:`RecallMeetingsConfig`'s participant map path."""
        return cls.from_file(config.participant_map_path, missing_ok=missing_ok)

    # ── matching ─────────────────────────────────────────────────────────────
    @property
    def is_empty(self) -> bool:
        return not self._people

    def match(
        self, *, display_name: str | None = None, email: str | None = None
    ) -> ParticipantMatch:
        """Return the exact-match verdict for ``email`` / ``display_name``.

        Email wins when it uniquely identifies a person; otherwise an exact
        alias match is used.  An email/alias shared by multiple people yields an
        ambiguous (non-taggable) result, and unknown input yields ``unmatched``.
        """
        email_clean = _clean_str(email)
        name_clean = _clean_str(display_name)
        if not email_clean and not name_clean:
            return _NO_INPUT

        if email_clean:
            bucket = self._by_email.get(_norm_email(email_clean))
            if bucket:
                if len(bucket) == 1:
                    return _match_from_person(
                        bucket[0], reason=MATCH_REASON_EMAIL, confidence=EMAIL_CONFIDENCE
                    )
                return _AMBIGUOUS

        if name_clean:
            bucket = self._by_alias.get(_norm_alias(name_clean))
            if bucket:
                if len(bucket) == 1:
                    return _match_from_person(
                        bucket[0], reason=MATCH_REASON_ALIAS, confidence=ALIAS_CONFIDENCE
                    )
                return _AMBIGUOUS

        return _UNMATCHED

    def to_meeting_participant(
        self,
        meeting_id: str,
        *,
        display_name: str | None = None,
        email: str | None = None,
        recall_participant_id: str | None = None,
    ) -> MeetingParticipant:
        """Resolve a participant and project the match onto a storable record."""
        match = self.match(display_name=display_name, email=email)
        return MeetingParticipant(
            meeting_id=meeting_id,
            raw_display_name=_clean_str(display_name),
            email=_clean_str(email),
            recall_participant_id=_clean_str(recall_participant_id),
            canonical_name=match.canonical_name,
            telegram_username=match.telegram_username,
            telegram_user_id=match.telegram_user_id,
            match_confidence=match.confidence if match.matched else None,
            match_reason=match.reason,
        )


# ── Telegram-ready formatting ────────────────────────────────────────────────
def format_mention(match: ParticipantMatch, *, fallback: str = DEFAULT_OWNER_FALLBACK) -> str:
    """Render a Telegram mention for a *taggable* match, else ``fallback``.

    A username becomes ``@handle``; a user-id-only person becomes a
    ``tg://user?id=…`` markdown link.  Non-taggable matches (unmatched /
    ambiguous / handle-less) never produce a mention — they return ``fallback``.
    """
    if not match.is_taggable:
        return fallback
    if match.telegram_username:
        return f"@{match.telegram_username.lstrip('@')}"
    name = match.canonical_name or "user"
    return f"[{name}](tg://user?id={match.telegram_user_id})"


def format_action_owner(
    directory: ParticipantDirectory,
    owner: str | None,
    *,
    unassigned: str = DEFAULT_OWNER_FALLBACK,
) -> str:
    """Render an action-item owner for Telegram.

    * Empty / ``unassigned``-like owners → ``unassigned``.
    * A confidently matched, taggable owner → an ``@mention`` / user-id link.
    * A named-but-not-taggable owner (unmatched or ambiguous) → the plain name,
      never a fabricated mention.
    """
    owner_clean = _clean_str(owner)
    if owner_clean is None or owner_clean.casefold() in _UNASSIGNED_TOKENS:
        return unassigned
    match = directory.match(display_name=owner_clean)
    if match.is_taggable:
        return format_mention(match)
    return owner_clean


__all__ = [
    "ALIAS_CONFIDENCE",
    "EMAIL_CONFIDENCE",
    "MATCH_REASON_ALIAS",
    "MATCH_REASON_EMAIL",
    "REASON_AMBIGUOUS",
    "REASON_NO_INPUT",
    "REASON_UNMATCHED",
    "DEFAULT_OWNER_FALLBACK",
    "Person",
    "ParticipantMatch",
    "ParticipantDirectory",
    "format_mention",
    "format_action_owner",
]
