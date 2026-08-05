"""Command-line entrypoints for the standalone meeting assistant."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Iterable
from urllib.parse import urlparse

from recall_meeting_assistant.client import RecallClient
from recall_meeting_assistant.config import RecallConfigError, load_config
from recall_meeting_assistant.runtime import load_env_file


def validate_meeting_url(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """Reject non-HTTPS or non-allowlisted meeting URLs before API calls."""

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or not host:
        raise ValueError("meeting_url must be an HTTPS URL")
    if not any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains):
        raise ValueError(
            "meeting_url host is not allowlisted; configure MEETING_ASSISTANT_ALLOWED_DOMAINS"
        )
    return True


def _status_from_payload(payload: dict[str, object]) -> str | None:
    changes = payload.get("status_changes")
    if isinstance(changes, list) and changes:
        last = changes[-1]
        if isinstance(last, dict) and last.get("code"):
            return str(last["code"])
    status = payload.get("status")
    if isinstance(status, dict) and status.get("code"):
        return str(status["code"])
    return str(status) if isinstance(status, str) and status else None


def _config_or_error():
    try:
        return load_config()
    except RecallConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return None


def create_bot(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Create a Recall.ai meeting bot")
    parser.add_argument("meeting_url")
    parser.add_argument("--name", default=None, help="bot display name")
    parser.add_argument("--language", default=None, help="transcription language code or auto")
    parser.add_argument("--mode", default=None, help="Recall transcription mode")
    args = parser.parse_args(argv)
    config = _config_or_error()
    if config is None:
        return 2
    try:
        validate_meeting_url(args.meeting_url, config.allowed_meeting_domains)
    except ValueError as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2

    client = RecallClient.from_config(config)
    try:
        bot = client.create_bot(
            args.meeting_url,
            bot_name=args.name or config.bot_name,
            language_code=args.language or config.language_code,
            transcription_mode=args.mode or config.transcription_mode,
        )
    except Exception as exc:  # noqa: BLE001 - CLI should return a safe provider error
        print(f"Recall request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(
        json.dumps(
            {"bot_id": bot.id, "status": bot.status, "region": config.recallai_region},
            ensure_ascii=False,
        )
    )
    return 0


def get_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Read a Recall.ai bot status")
    parser.add_argument("bot_id")
    args = parser.parse_args(argv)
    config = _config_or_error()
    if config is None:
        return 2
    client = RecallClient.from_config(config)
    try:
        payload = client.get_bot(args.bot_id)
    except Exception as exc:  # noqa: BLE001
        print(f"Recall request failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(
        json.dumps(
            {"bot_id": args.bot_id, "status": _status_from_payload(payload)},
            ensure_ascii=False,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Recall.ai meeting assistant")
    subparsers = parser.add_subparsers(dest="command")
    create = subparsers.add_parser("create-bot", help="create a bot for an HTTPS meeting URL")
    create.add_argument("meeting_url")
    create.add_argument("--name", default=None)
    create.add_argument("--language", default=None)
    create.add_argument("--mode", default=None)
    status = subparsers.add_parser("status", help="get a bot status")
    status.add_argument("bot_id")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    load_env_file()
    args = list(argv) if argv is not None else None
    parser = build_parser()
    parsed = parser.parse_args(args)
    if parsed.command == "create-bot":
        command_args = [parsed.meeting_url]
        if parsed.name:
            command_args.extend(["--name", parsed.name])
        if parsed.language:
            command_args.extend(["--language", parsed.language])
        if parsed.mode:
            command_args.extend(["--mode", parsed.mode])
        return create_bot(command_args)
    if parsed.command == "status":
        return get_status([parsed.bot_id])
    parser.print_help()
    return 2


def create_bot_main(argv: Iterable[str] | None = None) -> int:
    load_env_file()
    return create_bot(list(argv) if argv is not None else sys.argv[1:])


def status_main(argv: Iterable[str] | None = None) -> int:
    load_env_file()
    return get_status(list(argv) if argv is not None else sys.argv[1:])


__all__ = [
    "build_parser",
    "create_bot",
    "create_bot_main",
    "get_status",
    "main",
    "status_main",
    "validate_meeting_url",
]

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
