# Architecture

## Scope

This repository is a standalone Recall.ai meeting assistant. It does not depend on
an agent runtime, personal messaging account, or a deployment-specific filesystem.
The production path is deliberately split into short, retryable stages:

```text
Recall.ai bot
    |
    v
HTTPS webhook receiver -- verifies signature --> private webhook JSON
                                                    |
                                                    v
                                           ingest runner / Recall API
                                                    |
                 +----------------------------------+----------------+
                 |                                  |                |
                 v                                  v                v
             SQLite DB                    normalized JSON       transcript.md
                                                    |
                                                    v
                                          durable Telegram outbox
                                                    |
                                                    v
                                             Telegram Bot API
```

## Components

- `client.py` contains the small Recall.ai HTTP client and create-bot payload
  builder. It never prints API keys or meeting URLs.
- `receiver.py` verifies Recall/Svix signatures over the raw request body and
  writes accepted events atomically. It does not make provider calls on the
  request path.
- `ingest_runner.py` consumes verified webhook files and calls `ingest.py`.
- `ingest.py` fetches the transcript, normalizes speaker/timestamp shapes, stores
  artifacts, and queues the complete transcript.
- `storage.py` owns the SQLite schema and private artifact paths.
- `telegram.py` is an optional delivery adapter. It turns Markdown-like text into
  safe Telegram HTML and splits long transcripts at line boundaries.
- `delivery.py` is provider-neutral durable outbox state management and is useful
  for tests or custom adapters.

## Idempotency

Webhook filenames and `MEETING_ASSISTANT_PROCESSED_FILE` prevent successful events
from being processed repeatedly. Each meeting has deterministic artifact names.
Outbox files remain `queued` or `failed` until a delivery succeeds. A `delivered`
outbox is skipped on later passes.

## Secret boundaries

- Recall API credentials are read from environment variables.
- Webhook verification happens before an event is persisted.
- Runtime paths default to a per-user data directory or explicit local paths.
- Logs and persisted delivery failures never include raw provider exceptions,
  signed URLs, or credentials.
- Meeting URLs are redacted before being stored in session metadata; raw provider
  payloads remain private runtime artifacts and are ignored by Git.

## Extension points

The ingest flow accepts a Recall client protocol and an injected fallback
transcriber. A different delivery channel can implement the `OutboxSender`
protocol and reuse `deliver_outbox_file`. A production deployment can replace the
stdlib receiver with a framework route while retaining the same verification and
file handoff contract.
