"""Environment-backed configuration for the standalone meeting assistant."""

from __future__ import annotations

import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Mapping

from recall_meeting_assistant.runtime import get_product_home, mask_secret

ENV_API_KEY = "RECALLAI_API_KEY"
ENV_REGION = "RECALLAI_REGION"
ENV_WEBHOOK_SECRET = "RECALLAI_WEBHOOK_SECRET"
ENV_WEBHOOK_TOKEN = "RECALL_WEBHOOK_TOKEN"
ENV_OPENAI_API_KEY = "OPENAI_API_KEY"
ENV_PUBLIC_BASE_URL = "MEETING_ASSISTANT_PUBLIC_BASE_URL"
ENV_ENV_FILE = "MEETING_ASSISTANT_ENV_FILE"
ENV_HOME = "MEETING_ASSISTANT_HOME"
ENV_STORAGE_DIR = "MEETING_ASSISTANT_STORAGE_DIR"
ENV_WEBHOOK_DIR = "MEETING_ASSISTANT_WEBHOOK_DIR"
ENV_PROCESSED_FILE = "MEETING_ASSISTANT_PROCESSED_FILE"
ENV_PARTICIPANT_MAP = "MEETING_ASSISTANT_PARTICIPANT_MAP"
ENV_TELEGRAM_BACKEND = "MEETING_ASSISTANT_TELEGRAM_BACKEND"
ENV_TELEGRAM_BOT_TOKEN = "TELEGRAM_BOT_TOKEN"
ENV_TELEGRAM_CHAT_ID = "MEETING_ASSISTANT_TELEGRAM_CHAT_ID"
ENV_TELEGRAM_THREAD_ID = "MEETING_ASSISTANT_TELEGRAM_THREAD_ID"
ENV_OPENAI_FALLBACK_MODEL = "MEETING_ASSISTANT_OPENAI_FALLBACK_MODEL"
ENV_SUMMARY_MODEL = "MEETING_ASSISTANT_SUMMARY_MODEL"
ENV_FALLBACK_POLICY = "MEETING_ASSISTANT_FALLBACK_POLICY"
ENV_COST_CAP = "MEETING_ASSISTANT_MONTHLY_COST_CAP_USD"
ENV_ALLOWED_DOMAINS = "MEETING_ASSISTANT_ALLOWED_DOMAINS"
ENV_BOT_NAME = "MEETING_ASSISTANT_BOT_NAME"
ENV_LANGUAGE_CODE = "MEETING_ASSISTANT_LANGUAGE_CODE"
ENV_TRANSCRIPTION_MODE = "MEETING_ASSISTANT_TRANSCRIPTION_MODE"

DEFAULT_REGION = "us-east-1"
DEFAULT_TELEGRAM_BACKEND = "telegram"
DEFAULT_OPENAI_FALLBACK_MODEL = "gpt-4o-transcribe"
DEFAULT_SUMMARY_MODEL = "gpt-4o"
DEFAULT_FALLBACK_POLICY = "auto"
DEFAULT_BOT_NAME = "Meeting Notetaker"
DEFAULT_LANGUAGE_CODE = "auto"
DEFAULT_TRANSCRIPTION_MODE = "prioritize_accuracy"
DEFAULT_ALLOWED_DOMAINS: tuple[str, ...] = ("meet.google.com",)

TELEGRAM_BACKENDS = ("telegram",)
FALLBACK_POLICIES = ("auto", "always", "never")

_SECRET_FIELDS = frozenset(
    {
        "recallai_api_key",
        "recallai_webhook_secret",
        "recall_webhook_token",
        "openai_api_key",
        "telegram_bot_token",
    }
)
_PRIVATE_FIELDS = _SECRET_FIELDS | frozenset({"telegram_chat_id", "telegram_thread_id"})


class RecallConfigError(ValueError):
    """Raised when configuration is missing or invalid."""

    def __init__(self, message: str, *, missing: list[str] | None = None) -> None:
        super().__init__(message)
        self.missing = list(missing or [])


def _clean(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _missing_message(missing: list[str]) -> str:
    return (
        "Missing required meeting-assistant configuration: "
        f"{', '.join(missing)}. Set these in .env or your deployment secret manager. "
        "See README.md. Never commit secret values."
    )


def _home_from_source(source: Mapping[str, str]) -> Path:
    explicit = _clean(source.get(ENV_HOME))
    if explicit:
        return Path(explicit).expanduser()
    xdg = _clean(source.get("XDG_DATA_HOME"))
    if xdg:
        return Path(xdg).expanduser() / "recall-meeting-assistant"
    return get_product_home()


@dataclass(frozen=True, repr=False)
class RecallMeetingsConfig:
    """Resolved configuration for Recall, local storage, and optional delivery."""

    # The Recall key is the only value required for the core create/ingest flow.
    recallai_api_key: str
    recallai_webhook_secret: str | None = None
    recall_webhook_token: str | None = None
    openai_api_key: str | None = None
    public_base_url: str | None = None

    recallai_region: str = DEFAULT_REGION
    telegram_backend: str = DEFAULT_TELEGRAM_BACKEND
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_thread_id: str | None = None

    storage_dir: Path = field(default_factory=lambda: get_product_home() / "meetings")
    webhook_dir: Path = field(default_factory=lambda: get_product_home() / "webhooks")
    processed_file: Path = field(
        default_factory=lambda: get_product_home() / "meetings" / "ingest_processed.json"
    )
    participant_map_path: Path = field(
        default_factory=lambda: get_product_home() / "participants.yaml"
    )

    openai_fallback_model: str = DEFAULT_OPENAI_FALLBACK_MODEL
    summary_model: str = DEFAULT_SUMMARY_MODEL
    fallback_policy: str = DEFAULT_FALLBACK_POLICY
    monthly_cost_cap_usd: float | None = None
    allowed_meeting_domains: tuple[str, ...] = DEFAULT_ALLOWED_DOMAINS
    bot_name: str = DEFAULT_BOT_NAME
    language_code: str = DEFAULT_LANGUAGE_CODE
    transcription_mode: str = DEFAULT_TRANSCRIPTION_MODE

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        require_webhook_secret: bool = False,
    ) -> "RecallMeetingsConfig":
        """Build a config from ``env`` without printing or embedding secrets.

        The webhook secret and OpenAI key are optional because creating bots and
        processing already-authenticated local fixtures do not need them. Use
        ``require_webhook_secret=True`` for a public webhook receiver.
        """

        source = os.environ if env is None else env

        def get(key: str) -> str | None:
            return _clean(source.get(key))

        api_key = get(ENV_API_KEY)
        webhook_secret = get(ENV_WEBHOOK_SECRET)
        webhook_token = get(ENV_WEBHOOK_TOKEN)
        openai_api_key = get(ENV_OPENAI_API_KEY)
        public_base_url = get(ENV_PUBLIC_BASE_URL)
        backend = (get(ENV_TELEGRAM_BACKEND) or DEFAULT_TELEGRAM_BACKEND).lower()

        if backend not in TELEGRAM_BACKENDS:
            raise RecallConfigError(
                f"{ENV_TELEGRAM_BACKEND} must be one of {', '.join(TELEGRAM_BACKENDS)}."
            )

        missing: list[str] = []
        if not api_key:
            missing.append(ENV_API_KEY)
        if require_webhook_secret and not (webhook_secret or webhook_token):
            missing.append(f"{ENV_WEBHOOK_SECRET} or {ENV_WEBHOOK_TOKEN}")
        if missing:
            raise RecallConfigError(_missing_message(missing), missing=missing)

        fallback_policy = (get(ENV_FALLBACK_POLICY) or DEFAULT_FALLBACK_POLICY).lower()
        if fallback_policy not in FALLBACK_POLICIES:
            raise RecallConfigError(
                f"{ENV_FALLBACK_POLICY} must be one of {', '.join(FALLBACK_POLICIES)}."
            )

        cost_cap_raw = get(ENV_COST_CAP)
        monthly_cost_cap: float | None = None
        if cost_cap_raw is not None:
            try:
                monthly_cost_cap = float(cost_cap_raw)
            except ValueError as exc:
                raise RecallConfigError(f"{ENV_COST_CAP} must be a number.") from exc
            if monthly_cost_cap < 0:
                raise RecallConfigError(f"{ENV_COST_CAP} must not be negative.")

        domains_raw = get(ENV_ALLOWED_DOMAINS)
        domains = (
            tuple(part.strip().lower() for part in domains_raw.split(",") if part.strip())
            if domains_raw
            else DEFAULT_ALLOWED_DOMAINS
        )
        home = _home_from_source(source)
        storage_dir = Path(get(ENV_STORAGE_DIR) or home / "meetings").expanduser()
        webhook_dir = Path(get(ENV_WEBHOOK_DIR) or home / "webhooks").expanduser()
        processed_file = Path(
            get(ENV_PROCESSED_FILE) or storage_dir / "ingest_processed.json"
        ).expanduser()
        participant_map = Path(
            get(ENV_PARTICIPANT_MAP) or home / "participants.yaml"
        ).expanduser()

        return cls(
            recallai_api_key=api_key,  # type: ignore[arg-type]
            recallai_webhook_secret=webhook_secret,
            recall_webhook_token=webhook_token,
            openai_api_key=openai_api_key,
            public_base_url=public_base_url,
            recallai_region=get(ENV_REGION) or DEFAULT_REGION,
            telegram_backend=backend,
            telegram_bot_token=get(ENV_TELEGRAM_BOT_TOKEN),
            telegram_chat_id=get(ENV_TELEGRAM_CHAT_ID),
            telegram_thread_id=get(ENV_TELEGRAM_THREAD_ID),
            storage_dir=storage_dir,
            webhook_dir=webhook_dir,
            processed_file=processed_file,
            participant_map_path=participant_map,
            openai_fallback_model=get(ENV_OPENAI_FALLBACK_MODEL)
            or DEFAULT_OPENAI_FALLBACK_MODEL,
            summary_model=get(ENV_SUMMARY_MODEL) or DEFAULT_SUMMARY_MODEL,
            fallback_policy=fallback_policy,
            monthly_cost_cap_usd=monthly_cost_cap,
            allowed_meeting_domains=domains,
            bot_name=get(ENV_BOT_NAME) or DEFAULT_BOT_NAME,
            language_code=get(ENV_LANGUAGE_CODE) or DEFAULT_LANGUAGE_CODE,
            transcription_mode=get(ENV_TRANSCRIPTION_MODE) or DEFAULT_TRANSCRIPTION_MODE,
        )

    def as_redacted_dict(self) -> dict[str, object]:
        """Return a representation safe for diagnostics."""

        result: dict[str, object] = {}
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in _SECRET_FIELDS:
                result[item.name] = mask_secret(value, empty="(not set)")
            elif item.name in _PRIVATE_FIELDS:
                result[item.name] = "(configured)" if value else "(not set)"
            elif isinstance(value, Path):
                result[item.name] = str(value)
            else:
                result[item.name] = value
        return result

    def __repr__(self) -> str:
        parts: list[str] = []
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name in _SECRET_FIELDS:
                shown = mask_secret(value, empty="(not set)")
            elif item.name in _PRIVATE_FIELDS:
                shown = "(configured)" if value else "(not set)"
            else:
                shown = repr(value)
            parts.append(f"{item.name}={shown}")
        return f"{type(self).__name__}({', '.join(parts)})"


def load_config(
    env: Mapping[str, str] | None = None, *, require_webhook_secret: bool = False
) -> RecallMeetingsConfig:
    """Load and validate configuration from ``env`` or the process environment."""

    return RecallMeetingsConfig.from_env(env, require_webhook_secret=require_webhook_secret)


__all__ = [
    "RecallConfigError",
    "RecallMeetingsConfig",
    "load_config",
    "DEFAULT_REGION",
    "DEFAULT_TELEGRAM_BACKEND",
    "DEFAULT_OPENAI_FALLBACK_MODEL",
    "DEFAULT_SUMMARY_MODEL",
    "DEFAULT_FALLBACK_POLICY",
    "DEFAULT_BOT_NAME",
    "DEFAULT_LANGUAGE_CODE",
    "DEFAULT_TRANSCRIPTION_MODE",
    "DEFAULT_ALLOWED_DOMAINS",
    "TELEGRAM_BACKENDS",
    "FALLBACK_POLICIES",
]
