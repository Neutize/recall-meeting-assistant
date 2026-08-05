# Security policy

## Reporting a vulnerability

Please do not open a public issue containing credentials, signed meeting URLs,
transcripts, or an exploit that can affect a live Recall.ai account. Contact the
repository maintainers through a private GitHub security advisory instead.

If private advisories are not enabled on the fork you are using, remove all
sensitive values from the report and open an issue with only a minimal
reproduction.

## Secret handling

- Copy `.env.example` to `.env`; never commit `.env`.
- Keep the Recall API key, webhook secret, Telegram token, and optional provider
  keys in a secret manager or process environment in production.
- Treat meeting URLs, webhook payloads, transcripts, and signed download URLs as
  private runtime data.
- The receiver verifies the webhook before writing it to disk. Put it behind
  HTTPS and restrict access to the health endpoint in production.
