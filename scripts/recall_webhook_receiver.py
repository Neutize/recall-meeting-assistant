#!/usr/bin/env python3
"""Compatibility entrypoint for the Recall webhook receiver."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from recall_meeting_assistant.receiver import (  # noqa: E402
    DEFAULT_HOST,
    DEFAULT_PORT,
    Handler,
    choose_secret,
    load_env,
    main,
    safe_id,
    serve,
    store_event,
    verify_recall_signature,
    webhook_store_dir,
)

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

if __name__ == "__main__":
    raise SystemExit(main())
