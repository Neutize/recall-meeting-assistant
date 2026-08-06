# Recall Meeting Assistant

A small, installable meeting assistant built around [Recall.ai](https://www.recall.ai/).
It joins a supported meeting, records it through Recall.ai, downloads the completed
transcript, stores local artifacts, and can deliver the full transcript to Telegram.

The repository is intentionally standalone. It contains no personal account data,
private chat IDs, API keys, OAuth sessions, or deployment-specific paths.

## What it does

1. Creates a Recall.ai bot for an HTTPS meeting URL.
2. Receives and verifies Recall.ai webhooks.
3. Fetches the completed transcript from Recall.ai.
4. Stores a normalized transcript in SQLite, JSON, and Markdown formats.
5. Queues a complete transcript message in a durable outbox.
6. Sends the outbox to Telegram in messages no larger than Telegram's limit.
7. Optionally persists a native Recall summary when the provider returns one.

The core library is dependency-injected, so it can also be embedded into another
HTTP service or delivery adapter.

## Requirements

- Python 3.11 or newer
- A Recall.ai account and API key
- A public HTTPS endpoint for production webhooks
- Optional: a Telegram bot token and destination chat/topic
- Optional: OpenAI credentials only if you wire the fallback transcriber

## Installation

```bash
git clone https://github.com/Neutize/recall-meeting-assistant.git
cd recall-meeting-assistant

uv venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"

cp .env.example .env
```

If `uv` is not available, use a normal virtual environment and install with
`python -m pip install -e ".[dev]"`.

Edit `.env` and set at least:

```dotenv
RECALLAI_API_KEY=replace-with-your-key
RECALLAI_REGION=us-east-1
RECALLAI_WEBHOOK_SECRET=replace-with-your-webhook-secret
```

For Telegram delivery also set:

```dotenv
TELEGRAM_BOT_TOKEN=replace-with-your-bot-token
MEETING_ASSISTANT_TELEGRAM_CHAT_ID=-1000000000000
# Set this only when delivering into a forum topic.
MEETING_ASSISTANT_TELEGRAM_THREAD_ID=42
```

Never commit `.env`, `data/`, recordings, raw webhook payloads, or participant
maps. The default `.gitignore` protects these paths.

## Run the assistant

### 1. Create a meeting bot

```bash
recall-meeting-create-bot "https://meet.google.com/your-meeting-code" \
  --name "Meeting Notetaker"
```

The command prints the new bot ID and status. The meeting host must allow the
Recall.ai bot to join. The default URL allowlist is `meet.google.com`; extend it
with `MEETING_ASSISTANT_ALLOWED_DOMAINS` only when you have tested the provider
configuration for another platform.

To inspect a bot later:

```bash
recall-meeting-status BOT_ID
```

### 2. Configure the Recall.ai webhook

In the Recall.ai dashboard, create a webhook pointing to:

```text
https://YOUR_PUBLIC_HOST/webhooks/recall-ai
```

Configure the corresponding Recall signing secret as
`RECALLAI_WEBHOOK_SECRET`. The receiver accepts `GET /healthz` for a basic health
check and writes only verified webhooks to `MEETING_ASSISTANT_WEBHOOK_DIR`.

For local development, a reverse proxy or tunnel must provide HTTPS. A query
parameter token (`RECALL_WEBHOOK_TOKEN`) is supported as a development fallback,
but signed webhooks are preferred for production.

### 3. Start the webhook receiver

```bash
recall-meeting-webhook-receiver --host 127.0.0.1 --port 8787
```

Put TLS, authentication/rate limiting, and process supervision in front of this
small HTTP server. The receiver deliberately does not call Recall.ai during the
HTTP request, so webhook delivery stays fast and retries are safe.

### 4. Ingest transcripts

Run this command on a schedule, or keep it under a service supervisor:

```bash
recall-meeting-ingest
```

It processes each verified webhook once. Failed files remain retryable. Meeting
artifacts are written under `MEETING_ASSISTANT_STORAGE_DIR` (by default
`./data/meetings`).

### 5. Deliver the transcript

With Telegram variables configured:

```bash
recall-meeting-deliver-outbox --storage-root ./data/meetings
```

The sender uses `sendMessage`, escapes HTML safely, and splits long transcripts
on line boundaries. A delivered outbox file is not sent again. A failed file is
kept retryable and stores only a generic failure reason, never the provider's
raw exception text.

Use a local fake sender to verify the outbox without network access:

```bash
recall-meeting-deliver-outbox-fake ./data/meetings --fake
```

Use `--dry-run` with the Telegram command to list pending messages without sending:

```bash
recall-meeting-deliver-outbox --storage-root ./data/meetings --dry-run
```

## Suggested service layout

The four stages can run as separate supervised processes or scheduled commands:

```text
Recall.ai -> HTTPS receiver -> data/webhooks/*.json -> ingest -> data/meetings/*
                                                         |
                                                         v
                                             Telegram outbox delivery
```

A cron-style setup can run `recall-meeting-ingest` and
`recall-meeting-deliver-outbox` every minute. Use a real process supervisor for
the receiver. Do not expose the development HTTP server directly to the public
internet without TLS and a firewall/reverse proxy.

## Configuration reference

| Variable | Required | Purpose |
| --- | --- | --- |
| `RECALLAI_API_KEY` | yes | Recall.ai API access |
| `RECALLAI_REGION` | no | Recall.ai region, default `us-east-1` |
| `RECALLAI_WEBHOOK_SECRET` | receiver | Recall/Svix signing secret |
| `RECALL_WEBHOOK_TOKEN` | dev only | Query-token fallback for local testing |
| `MEETING_ASSISTANT_HOME` | no | Base directory for private runtime data |
| `MEETING_ASSISTANT_STORAGE_DIR` | no | Meeting database and artifacts |
| `MEETING_ASSISTANT_WEBHOOK_DIR` | no | Verified webhook files |
| `MEETING_ASSISTANT_PROCESSED_FILE` | no | Ingest idempotency state |
| `MEETING_ASSISTANT_BOT_NAME` | no | Default bot name |
| `MEETING_ASSISTANT_LANGUAGE_CODE` | no | Recall language code, default `auto` |
| `MEETING_ASSISTANT_ALLOWED_DOMAINS` | no | Comma-separated meeting host allowlist |
| `TELEGRAM_BOT_TOKEN` | Telegram | Bot API token |
| `MEETING_ASSISTANT_TELEGRAM_CHAT_ID` | Telegram | Default destination chat |
| `MEETING_ASSISTANT_TELEGRAM_THREAD_ID` | no | Forum topic ID |
| `OPENAI_API_KEY` | no | Optional injected fallback provider |
| `MEETING_ASSISTANT_FALLBACK_POLICY` | no | `auto`, `always`, or `never` |

All configuration can be supplied by the process environment. Commands also load
an adjacent `.env` file without overriding variables already present in the
process environment.

## Local development

Run the tests and lint checks:

```bash
uv pip install -e ".[dev]"
python -m pytest -q
ruff check src tests scripts
```

The test suite uses fake Recall responses and a fake Telegram poster. It does not
need a live API key or a real meeting. A local end-to-end smoke path is:

```bash
cp .env.example .env
# Set only a test RECALLAI_API_KEY if exercising the CLI validation path.
recall-meeting-deliver-outbox-fake ./data/meetings --fake
```

## Privacy and security

- Keep API keys and webhook secrets in a secret manager or an ignored `.env`.
- Treat raw transcripts, recordings, participant maps, and webhook files as
  private meeting data.
- Do not put signed Recall URLs in issue reports or logs.
- Use a dedicated Telegram bot and destination with the minimum required access.
- Review local retention and deletion requirements before using the assistant for
  sensitive meetings.

## License

MIT. See [LICENSE](LICENSE).
