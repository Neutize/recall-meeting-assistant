"""Small HTTP receiver for Recall.ai dashboard webhooks.

The receiver verifies Svix/Recall signatures before writing a private webhook
file. It intentionally does not download transcripts on the request path. Run
it behind a TLS reverse proxy or another HTTPS edge.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import hmac
import json
import logging
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Mapping, cast
from urllib.parse import parse_qs, urlparse

from recall_meeting_assistant.runtime import get_product_home, read_env_file

logger = logging.getLogger(__name__)
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8787
DEFAULT_MAX_BODY_BYTES = 2_000_000
DEFAULT_TIMESTAMP_TOLERANCE_SECONDS = 5 * 60
WEBHOOK_PATHS = {"/webhooks/recall-ai", "/webhooks/recall-ai/"}


def load_env(path: str | Path | None = None) -> dict[str, str]:
    """Combine a local env file with the process environment, with env winning."""

    env_path = path or os.environ.get("MEETING_ASSISTANT_ENV_FILE", ".env")
    values = read_env_file(env_path)
    values.update(os.environ)
    return values


def choose_secret(env: Mapping[str, str]) -> str | None:
    """Return the first configured Recall/Svix ``whsec_`` secret."""

    for key in (
        "RECALLAI_WEBHOOK_SECRET",
        "RECALL_SVIX_WEBHOOK_SECRET",
        "RECALL_WORKSPACE_VERIFICATION_SECRET",
    ):
        value = str(env.get(key) or "").strip()
        if value.startswith("whsec_"):
            return value
    return None


def verify_recall_signature(
    headers: Mapping[str, str],
    body: bytes,
    secret: str,
    *,
    now: float | None = None,
    tolerance_seconds: int = DEFAULT_TIMESTAMP_TOLERANCE_SECONDS,
) -> tuple[bool, str]:
    """Verify a Recall/Svix ``webhook-*`` signature over the raw body."""

    if not secret or not secret.startswith("whsec_"):
        return False, "missing_or_invalid_secret"
    msg_id = headers.get("webhook-id") or headers.get("svix-id")
    msg_ts = headers.get("webhook-timestamp") or headers.get("svix-timestamp")
    msg_sig = headers.get("webhook-signature") or headers.get("svix-signature")
    if not msg_id or not msg_ts or not msg_sig:
        return False, "missing_signature_headers"
    try:
        timestamp = int(str(msg_ts))
    except ValueError:
        return False, "invalid_timestamp"
    clock = time.time() if now is None else now
    if abs(clock - timestamp) > tolerance_seconds:
        return False, "timestamp_outside_tolerance"
    try:
        key = base64.b64decode(secret[len("whsec_") :], validate=True)
    except Exception:  # noqa: BLE001 - return a safe reason
        return False, "secret_base64_decode_failed"

    try:
        payload = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return False, "body_not_utf8"
    signed = f"{msg_id}.{msg_ts}.{payload}".encode("utf-8")
    expected = hmac.new(key, signed, hashlib.sha256).digest()
    for part in str(msg_sig).split():
        if "," not in part:
            continue
        version, signature = part.split(",", 1)
        if version != "v1":
            continue
        try:
            candidate = base64.b64decode(signature, validate=True)
        except Exception:  # noqa: BLE001 - try the next signature
            continue
        if len(candidate) == len(expected) and hmac.compare_digest(candidate, expected):
            return True, "ok"
    return False, "signature_mismatch"


def safe_id(value: str | None) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value or "no-id")[:120]


def webhook_store_dir(env: Mapping[str, str] | None = None) -> Path:
    values = env or load_env()
    explicit = str(values.get("MEETING_ASSISTANT_WEBHOOK_DIR") or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    storage = str(values.get("MEETING_ASSISTANT_STORAGE_DIR") or "").strip()
    if storage:
        return Path(storage).expanduser() / "webhooks"
    return get_product_home() / "webhooks"


def store_event(
    headers: Mapping[str, str],
    body: bytes,
    *,
    store_dir: str | Path | None = None,
) -> Path:
    """Write a verified webhook for the separate ingest process."""

    directory = Path(store_dir) if store_dir is not None else webhook_store_dir()
    directory.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    message_id = headers.get("webhook-id") or headers.get("svix-id")
    message_id = message_id or hashlib.sha256(body).hexdigest()[:16]
    try:
        parsed_body = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        parsed_body = None
    document = {
        "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "message_id": message_id,
        "event": parsed_body.get("event") if isinstance(parsed_body, dict) else None,
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "headers": {
            "webhook-id": headers.get("webhook-id") or headers.get("svix-id"),
            "webhook-timestamp": headers.get("webhook-timestamp") or headers.get("svix-timestamp"),
            "user-agent": headers.get("user-agent"),
        },
        "body": parsed_body if parsed_body is not None else body.decode("utf-8", errors="replace"),
    }
    path = directory / f"{now}_{safe_id(message_id)}.json"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    temporary.replace(path)
    return path


class Handler(BaseHTTPRequestHandler):
    server_version = "recall-meeting-assistant-webhook/1"

    def _json(self, status: int, payload: dict[str, object]) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: object) -> None:
        message = format % args
        parsed = urlparse(self.path)
        if parsed.query:
            message = message.replace(self.path, parsed.path)
        logger.info("webhook receiver: %s", message)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path in {"/health", "/healthz"}:
            env = load_env()
            self._json(
                200,
                {
                    "status": "ok",
                    "service": "recall-meeting-assistant-webhook",
                    "verification_secret_configured": bool(choose_secret(env)),
                },
            )
            return
        self._json(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        parsed = urlparse(self.path)
        if parsed.path not in WEBHOOK_PATHS:
            self._json(404, {"error": "not_found"})
            return
        try:
            length = int(self.headers.get("content-length") or 0)
        except ValueError:
            self._json(400, {"error": "invalid_content_length"})
            return
        max_body = int(load_env().get("MEETING_ASSISTANT_MAX_WEBHOOK_BYTES") or DEFAULT_MAX_BODY_BYTES)
        if length < 0 or length > max_body:
            self._json(413, {"error": "payload_too_large"})
            return
        body = self.rfile.read(length)
        env = load_env()
        headers = cast(Mapping[str, str], self.headers)
        secret = choose_secret(env)
        ok = False
        reason = "verification_secret_not_configured"
        if secret:
            ok, reason = verify_recall_signature(headers, body, secret)
        else:
            expected = str(env.get("RECALL_WEBHOOK_TOKEN") or "")
            supplied = (parse_qs(parsed.query).get("token") or [""])[0]
            if expected and supplied and hmac.compare_digest(expected, supplied):
                ok, reason = True, "query_token_ok"
        if not ok:
            self._json(401, {"error": "verification_failed", "reason": reason})
            return
        path = store_event(headers, body, store_dir=webhook_store_dir(env))
        logger.info("verified webhook stored: %s", path.name)
        self.send_response(204)
        self.end_headers()


def serve(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Run the blocking webhook HTTP server."""

    httpd = ThreadingHTTPServer((host, port), Handler)
    logger.info("Recall webhook receiver listening on %s:%s", host, port)
    try:
        httpd.serve_forever()
    finally:
        httpd.server_close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Receive Recall.ai webhooks")
    parser.add_argument("--host", default=os.environ.get("MEETING_ASSISTANT_WEBHOOK_HOST", DEFAULT_HOST))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("MEETING_ASSISTANT_WEBHOOK_PORT", DEFAULT_PORT)),
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("MEETING_ASSISTANT_LOG_LEVEL", "INFO"))
    serve(host=args.host, port=args.port)
    return 0


__all__ = [
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "Handler",
    "choose_secret",
    "load_env",
    "main",
    "safe_id",
    "serve",
    "store_event",
    "verify_recall_signature",
    "webhook_store_dir",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
