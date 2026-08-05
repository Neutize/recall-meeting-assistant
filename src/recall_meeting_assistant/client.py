"""Recall.ai HTTP client for the meeting-assistant package.

Thin wrapper over the Recall.ai REST API (Phase 1: bot creation).  See
docs/architecture.md.

Two invariants hold here:

1. **No secrets in logs / errors / repr.**  The API key lives only in the
   ``Authorization`` header and is never logged, echoed in exceptions, or
   rendered by ``repr``. Provider response bodies are not copied into errors.
   Meeting URLs are access-granting, so they are not logged.
2. **Typed errors.**  Non-2xx responses raise a :class:`RecallAPIError`
   subclass keyed on status: 400 → :class:`RecallBadRequestError`,
   401 → :class:`RecallAuthError`, 507 → :class:`RecallInsufficientStorageError`.

Recall exposes regional API hosts (``https://{region}.recall.ai``); the create
bot endpoint is ``POST /api/v1/bot/``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Mapping

from recall_meeting_assistant.config import DEFAULT_BOT_NAME, DEFAULT_LANGUAGE_CODE, DEFAULT_REGION

if TYPE_CHECKING:
    import httpx

    from recall_meeting_assistant.config import RecallMeetingsConfig

logger = logging.getLogger(__name__)

# ── Endpoint / request constants ─────────────────────────────────────────────
RECALL_API_VERSION = "v1"
#: Recall regional REST endpoints accept API keys as ``Authorization: Token``.
AUTH_SCHEME = "Token"
DEFAULT_TIMEOUT = 30.0
#: Post-meeting accuracy is the default; callers can opt into low latency.
DEFAULT_TRANSCRIPT_MODE = "prioritize_accuracy"

# Default automatic-leave timeouts in seconds.
_DEFAULT_AUTOMATIC_LEAVE = {
    "waiting_room_timeout": 600,
    "noone_joined_timeout": 900,
    "everyone_left_timeout": 120,
    "in_call_recording_timeout": 14400,
}

# ── Errors ───────────────────────────────────────────────────────────────────
class RecallAPIError(Exception):
    """A Recall.ai API call failed.

    ``status_code`` is the HTTP status (``None`` for transport-level errors).
    ``response_detail`` is retained for API compatibility and is always ``None``
    for provider responses, because arbitrary response bodies may contain
    secrets or meeting data.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        response_detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response_detail = response_detail


class RecallBadRequestError(RecallAPIError):
    """HTTP 400 — the request payload was rejected (e.g. invalid meeting URL)."""


class RecallAuthError(RecallAPIError):
    """HTTP 401 — the API key was missing, invalid, or revoked."""


class RecallInsufficientStorageError(RecallAPIError):
    """HTTP 507 — Recall could not provision the bot (capacity/concurrency)."""


_STATUS_ERRORS: dict[int, type[RecallAPIError]] = {
    400: RecallBadRequestError,
    401: RecallAuthError,
    507: RecallInsufficientStorageError,
}


# ── Helpers ──────────────────────────────────────────────────────────────────
def region_base_url(region: str | None) -> str:
    """Return the Recall.ai regional API base URL for ``region``.

    Blank/``None`` falls back to :data:`DEFAULT_REGION`.  Example::

        region_base_url("us-east-1")  ->  "https://us-east-1.recall.ai"
    """
    normalized = (region or "").strip().lower() or DEFAULT_REGION
    return f"https://{normalized}.recall.ai"


def build_create_bot_payload(
    meeting_url: str,
    *,
    bot_name: str = DEFAULT_BOT_NAME,
    metadata: Mapping[str, Any] | None = None,
    language_code: str = DEFAULT_LANGUAGE_CODE,
    transcription_mode: str = DEFAULT_TRANSCRIPT_MODE,
) -> dict[str, Any]:
    """Build the Recall create-bot request body.

    Mirrors the product payload: streaming transcription with diarization and
    automatic-leave timeouts. Caller ``metadata`` is merged over the defaults.
    """
    if not str(meeting_url or "").strip():
        raise ValueError("meeting_url is required to create a Recall bot.")

    merged_metadata: dict[str, Any] = {"source": "recall-meeting-assistant"}
    if metadata:
        merged_metadata.update(metadata)

    return {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_config": {
            "transcript": {
                "provider": {
                    "recallai_streaming": {
                        "mode": transcription_mode,
                        "language_code": language_code,
                    }
                },
                "diarization": {
                    "use_separate_streams_when_available": True,
                },
            },
        },
        "automatic_leave": dict(_DEFAULT_AUTOMATIC_LEAVE),
        "metadata": merged_metadata,
    }


def _latest_status(payload: dict[str, Any]) -> str | None:
    """Best-effort extract the bot's current status code from a bot payload."""
    changes = payload.get("status_changes")
    if isinstance(changes, list) and changes:
        last = changes[-1]
        if isinstance(last, dict) and last.get("code"):
            return str(last["code"])
    status = payload.get("status")
    if isinstance(status, dict) and status.get("code"):
        return str(status["code"])
    if isinstance(status, str) and status:
        return status
    return None


# ── Bot record ───────────────────────────────────────────────────────────────
@dataclass
class RecallBot:
    """A bot created via the Recall API."""

    id: str
    status: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_api(cls, payload: Any) -> "RecallBot":
        if not isinstance(payload, dict):
            raise RecallAPIError("Recall create-bot response was not a JSON object.")
        bot_id = payload.get("id")
        if not bot_id:
            raise RecallAPIError("Recall create-bot response is missing a bot id.")
        return cls(id=str(bot_id), status=_latest_status(payload), raw=payload)


# ── Client ───────────────────────────────────────────────────────────────────
class RecallClient:
    """Synchronous Recall.ai API client.

    Construct with an API key + region, or via :meth:`from_config`.  Tests
    inject an ``httpx.MockTransport`` via ``transport`` so no real network
    call is ever made.
    """

    def __init__(
        self,
        api_key: str,
        *,
        region: str = DEFAULT_REGION,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        transport: "httpx.BaseTransport | None" = None,
    ) -> None:
        import httpx

        if not str(api_key or "").strip():
            raise ValueError("A Recall API key is required.")

        self.region = region
        self.base_url = (base_url or region_base_url(region)).rstrip("/")
        self.bot_endpoint = f"{self.base_url}/api/{RECALL_API_VERSION}/bot/"

        # The key lives only in this header; never stored as a plain attribute.
        self._http = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={
                "Authorization": f"{AUTH_SCHEME} {api_key}",
                "Accept": "application/json",
            },
        )

    @classmethod
    def from_config(
        cls,
        config: "RecallMeetingsConfig",
        *,
        timeout: float = DEFAULT_TIMEOUT,
        transport: "httpx.BaseTransport | None" = None,
    ) -> "RecallClient":
        """Build a client from a :class:`RecallMeetingsConfig`."""
        return cls(
            config.recallai_api_key,
            region=config.recallai_region,
            timeout=timeout,
            transport=transport,
        )

    # ── API methods ──────────────────────────────────────────────────────────
    def _get_json(self, endpoint_path: str) -> dict[str, Any]:
        """GET a Recall API endpoint path and return its JSON object body."""
        url = f"{self.base_url}{endpoint_path}"
        logger.info("Recall GET → %s", url)
        try:
            response = self._http.get(url)
        except Exception as exc:  # noqa: BLE001 — httpx transport-level failures
            import httpx

            if isinstance(exc, httpx.HTTPError):
                raise RecallAPIError("Recall request failed: transport_error") from exc
            raise
        if 200 <= response.status_code < 300:
            payload = response.json()
            if not isinstance(payload, dict):
                raise RecallAPIError("Recall API response was not a JSON object.")
            return payload
        raise self._build_error(response)

    def get_bot(self, bot_id: str) -> dict[str, Any]:
        """Retrieve a Recall bot by id (``GET /api/v1/bot/{id}/``)."""
        if not str(bot_id or "").strip():
            raise ValueError("bot_id is required.")
        return self._get_json(f"/api/{RECALL_API_VERSION}/bot/{bot_id}/")

    def get_recording_resource(
        self,
        resource_id: str,
        *,
        resource_type: str = "transcript_artifact",
        include_transcript: bool = True,
    ) -> dict[str, Any]:
        """Retrieve a recording-adjacent resource needed by ingest.

        Phase 1 uses transcript artifacts, backed by Recall's
        ``GET /api/v1/transcript/{id}/`` endpoint.  ``include_transcript`` is
        accepted for protocol compatibility with the MCP tool; the REST endpoint
        returns the transcript data in its response body.
        """
        if not str(resource_id or "").strip():
            raise ValueError("resource_id is required.")
        if resource_type not in {"transcript_artifact", "transcript"}:
            raise ValueError(f"Unsupported Recall resource_type: {resource_type!r}")
        return self._get_json(f"/api/{RECALL_API_VERSION}/transcript/{resource_id}/")

    def create_bot(
        self,
        meeting_url: str,
        *,
        bot_name: str = DEFAULT_BOT_NAME,
        metadata: Mapping[str, Any] | None = None,
        language_code: str = DEFAULT_LANGUAGE_CODE,
        transcription_mode: str = DEFAULT_TRANSCRIPT_MODE,
    ) -> RecallBot:
        """Create a Recall bot for ``meeting_url``.

        Returns a :class:`RecallBot` on success (HTTP 2xx).  Raises a typed
        :class:`RecallAPIError` subclass on 400/401/507, or the base error for
        any other non-2xx status / transport failure.
        """
        payload = build_create_bot_payload(
            meeting_url,
            bot_name=bot_name,
            metadata=metadata,
            language_code=language_code,
            transcription_mode=transcription_mode,
        )
        # Endpoint host/path carry no secrets; the meeting URL is omitted.
        logger.info("Recall create_bot → %s", self.bot_endpoint)
        try:
            response = self._http.post(self.bot_endpoint, json=payload)
        except Exception as exc:  # noqa: BLE001 — httpx transport-level failures
            import httpx

            if isinstance(exc, httpx.HTTPError):
                raise RecallAPIError("Recall request failed: transport_error") from exc
            raise
        return self._handle_create_bot_response(response)

    # ── Response handling ─────────────────────────────────────────────────────
    def _handle_create_bot_response(self, response: "httpx.Response") -> RecallBot:
        status = response.status_code
        if 200 <= status < 300:
            bot = RecallBot.from_api(response.json())
            logger.info("Recall bot created (id=%s, status=%s)", bot.id, bot.status)
            return bot
        raise self._build_error(response)

    def _build_error(self, response: "httpx.Response") -> RecallAPIError:
        status = response.status_code
        message = f"Recall API request failed with HTTP {status}"

        exc_cls = _STATUS_ERRORS.get(status, RecallAPIError)
        return exc_cls(message, status_code=status, response_detail=None)

    # ── Lifecycle ──────────────────────────────────────────────────────────────
    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "RecallClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        # Never render the API key (it lives only in the request header).
        return f"{type(self).__name__}(base_url={self.base_url!r})"


__all__ = [
    "RECALL_API_VERSION",
    "AUTH_SCHEME",
    "DEFAULT_TIMEOUT",
    "DEFAULT_BOT_NAME",
    "DEFAULT_TRANSCRIPT_MODE",
    "DEFAULT_LANGUAGE_CODE",
    "RecallAPIError",
    "RecallBadRequestError",
    "RecallAuthError",
    "RecallInsufficientStorageError",
    "RecallBot",
    "RecallClient",
    "region_base_url",
    "build_create_bot_payload",
]
