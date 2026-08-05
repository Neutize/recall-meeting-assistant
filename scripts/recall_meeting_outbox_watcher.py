#!/usr/bin/env python3
"""Compatibility entrypoint for the Telegram outbox delivery adapter.

The implementation lives in ``recall_meeting_assistant.telegram`` so it is
available both after package installation and when this repository is run from
a checkout.
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from recall_meeting_assistant.runtime import load_env_file  # noqa: E402
from recall_meeting_assistant.telegram import (  # noqa: E402
    DEFAULT_BACKEND,
    DEFAULT_STORAGE_DIR,
    DELIVERED_STATUS,
    FAILED_STATUS,
    QUEUED_STATUS,
    OutboxPayload,
    build_parser,
    iter_pending,
    main,
    make_telegram_sender,
    run,
    split_telegram_text,
    telegram_api_post,
    telegram_markdown_to_html,
)

__all__ = [
    "DEFAULT_BACKEND",
    "DEFAULT_STORAGE_DIR",
    "DELIVERED_STATUS",
    "FAILED_STATUS",
    "QUEUED_STATUS",
    "OutboxPayload",
    "build_parser",
    "iter_pending",
    "load_env_file",
    "main",
    "make_telegram_sender",
    "run",
    "split_telegram_text",
    "telegram_api_post",
    "telegram_markdown_to_html",
]


if __name__ == "__main__":
    raise SystemExit(main())
