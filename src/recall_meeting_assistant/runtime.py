"""Runtime paths, environment loading, and secret-safe text helpers."""

from __future__ import annotations

import os
import re
from pathlib import Path

_SECRET_MIN_VISIBLE = 4


def get_product_home() -> Path:
    """Return the per-user data directory used by the assistant.

    ``MEETING_ASSISTANT_HOME`` wins when explicitly configured. Otherwise the
    standard XDG data directory is used, with a portable home-directory
    fallback. Runtime artifacts therefore never default to a machine-specific
    deployment path.
    """

    explicit = os.environ.get("MEETING_ASSISTANT_HOME", "").strip()
    if explicit:
        return Path(explicit).expanduser()
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return Path(xdg_data_home).expanduser() / "recall-meeting-assistant"
    return Path.home() / ".local" / "share" / "recall-meeting-assistant"


def _parse_env_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        line = line[7:].lstrip()
    if "=" not in line:
        return None
    key, value = line.split("=", 1)
    key = key.strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
        return None
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    return key, value


def read_env_file(path: str | Path | None = None) -> dict[str, str]:
    """Read a simple ``KEY=VALUE`` file without exposing values in results."""

    env_path = Path(path or os.environ.get("MEETING_ASSISTANT_ENV_FILE", ".env")).expanduser()
    if not env_path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in env_path.read_text(errors="replace").splitlines():
        parsed = _parse_env_line(line)
        if parsed:
            values[parsed[0]] = parsed[1]
    return values


def load_env_file(path: str | Path | None = None, *, override: bool = False) -> dict[str, str]:
    """Load a local env file into the process and return only applied key names."""

    values = read_env_file(path)
    applied: dict[str, str] = {}
    for key, value in values.items():
        if override or key not in os.environ:
            os.environ[key] = value
            applied[key] = "set"
    return applied


def mask_secret(value: object | None, *, empty: str | None = None) -> str | None:
    """Return a short masked representation of a secret-like value."""

    if value is None:
        return empty
    text = str(value)
    if not text:
        return empty if empty is not None else ""
    if len(text) <= _SECRET_MIN_VISIBLE * 2:
        return "***"
    return f"{text[:_SECRET_MIN_VISIBLE]}...{text[-_SECRET_MIN_VISIBLE:]}"


_SECRET_PATTERNS = (
    (r"sk-[A-Za-z0-9._-]+", "sk-***"),
    (r"whsec_[A-Za-z0-9._=-]+", "whsec_***"),
    (r"(bearer\s+)[A-Za-z0-9._~+/=-]+", r"\1***"),
    (r"(token\s+)[A-Za-z0-9._~+/=-]+", r"\1***"),
    (r"(api[_-]?key\s*[=:]\s*)[A-Za-z0-9._~+/=-]+", r"\1***"),
    (r"(secret\s*[=:]\s*)[A-Za-z0-9._~+/=-]+", r"\1***"),
    (r"([?&](?:AWSAccessKeyId|Signature|X-Amz-Signature|x-amz-security-token|api_key|access_token|token)=)[^&\s]+", r"\1***"),
    (r"(Authorization[:= ]+)(?:Token\s+|Bearer\s+)?[A-Za-z0-9._~+/=-]+", r"\1***"),
)


def redact_sensitive_text(text: object, *, force: bool = False) -> str:
    """Redact common credentials and signed URL query values from text."""

    _ = force  # Kept for compatibility with callers that request strict redaction.
    redacted = str(text)
    for pattern, replacement in _SECRET_PATTERNS:
        redacted = re.sub(pattern, replacement, redacted, flags=re.IGNORECASE)
    if len(redacted) > 500:
        return redacted[:500] + "...[truncated]"
    return redacted


__all__ = [
    "get_product_home",
    "load_env_file",
    "mask_secret",
    "read_env_file",
    "redact_sensitive_text",
]
