#!/usr/bin/env python3
"""Compatibility entrypoint for webhook ingestion."""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from recall_meeting_assistant.ingest_runner import (  # noqa: E402
    iter_webhook_files,
    load_processed,
    main,
    process_file,
    read_payload,
    save_processed,
)

__all__ = [
    "iter_webhook_files",
    "load_processed",
    "main",
    "process_file",
    "read_payload",
    "save_processed",
]

if __name__ == "__main__":
    raise SystemExit(main())
