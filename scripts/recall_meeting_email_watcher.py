#!/usr/bin/env python3
"""Compatibility entrypoint for Gmail summary email delivery."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from recall_meeting_assistant.email_delivery import (  # noqa: E402
    DEFAULT_STORAGE_DIR,
    DEFAULT_TOKEN_PATH,
    GMAIL_BACKEND,
    GMAIL_SCOPES,
    build_email_message,
    build_gmail_service,
    iter_pending,
    load_gmail_credentials,
    main,
    make_gmail_sender,
    run,
    summary_markdown_to_email_html,
)

__all__ = [
    "DEFAULT_STORAGE_DIR",
    "DEFAULT_TOKEN_PATH",
    "GMAIL_BACKEND",
    "GMAIL_SCOPES",
    "build_email_message",
    "build_gmail_service",
    "iter_pending",
    "load_gmail_credentials",
    "main",
    "make_gmail_sender",
    "run",
    "summary_markdown_to_email_html",
]


if __name__ == "__main__":
    raise SystemExit(main())
