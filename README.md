# Ledger Inbox

A self-hosted image inbox for bookkeeping automation. Send payment screenshots or
receipts through HTTPS, IMAP, or a signed email webhook; recognize them with a
replaceable vision adapter, review uncertain entries, and deliver them to a
replaceable ledger sink.

**Version 0.2.0 — Alpha.** This release supplies a complete API/CLI review workflow,
strict payment validation, crash recovery, and operation-bound ledger confirmation.
It is a single-host service for developers and self-hosters. See
[CHANGELOG](CHANGELOG.md) and [upgrade/operations](docs/operations.md).

```text
iPhone / Android / scanner / another service ── HTTPS ─┐
email alias ── IMAP or signed RFC822 webhook ──────────┼─> private durable inbox
                                                     └─> recognizer → review → ledger
```

Transport, model, and ledger are independent. Other channels can call the upload
API or implement a connector; no iCloud, Apple device, particular model vendor,
or particular accounting app is required.

## Quick start

Python 3.11+ and [uv](https://docs.astral.sh/uv/) are required:

```bash
git clone https://github.com/Keith-Jones7/ledger-inbox-public.git
cd ledger-inbox-public
uv sync --locked --extra dev
uv run ledger-inbox init
uv run ledger-inbox create-tenant --name "My ledger"
```

Save the returned tenant ID and API key privately. The API key is printed once;
keep it out of source control and avoid writing its literal value into shell history.
Start the API and worker in separate terminals:

```bash
uv run ledger-inbox serve --host 127.0.0.1 --port 8080
uv run ledger-inbox worker
```

Set `LEDGER_INBOX_CLIENT_KEY` privately, then upload a real image:

```bash
curl --fail-with-body \
  -H "X-Ledger-Inbox-Key: $LEDGER_INBOX_CLIENT_KEY" \
  -H "Idempotency-Key: one-stable-client-operation" \
  -F "image=@receipt.png" \
  http://127.0.0.1:8080/v1/ingest/images
```

Retry the **same** upload with the same idempotency key. A different image with
that key returns 409; identical image content is deduplicated per tenant across
channels. Supported inputs are decoded PNG, JPEG, WebP, HEIC, and AVIF; animated,
damaged, oversized, and excessive-pixel images are rejected.

The default `mock` adapter safely routes every image to review. This is a usable
review-first workflow, not a model that pretends to recognize screenshots.

## Complete a review

Inspect the event and original image through the authenticated API:

```bash
curl -H "X-Ledger-Inbox-Key: $LEDGER_INBOX_CLIENT_KEY" \
  http://127.0.0.1:8080/v1/events/EVENT_ID

uv run ledger-inbox review export EVENT_ID --tenant-id TENANT_UUID
```

The export includes the current `version`, private local original-image path,
and shared review policy. Create `corrected-entry.json` from the actual image:

```json
{
  "transaction_type": "expense",
  "amount_minor": 1299,
  "currency": "CNY",
  "merchant": "Example Cafe",
  "occurred_at": "2026-10-09T12:00:00+08:00",
  "category": "food",
  "needs_review": false,
  "review_reasons": []
}
```

Approve with the exact version from the export:

```bash
uv run ledger-inbox review approve EVENT_ID --tenant-id TENANT_UUID \
  --expected-version VERSION --entry-file corrected-entry.json
```

The worker delivers the approved entry once. By default the sink creates one
private, immutable JSON file under `data/ledger-outbox/`. That is confirmation
of the local output file; a separate accounting app needs its own sink/consumer.

Also available: `review list`, `ignore`, `retry`, and `reconcile`. Approvals and
ignores reject stale/repeated actions. Retry applies only to recognition failures,
which precede ledger dispatch. See [review API and CLI](docs/review.md).

## Vision models and optional independent verification

```bash
export LEDGER_INBOX_RECOGNIZER=openai-compatible
export LEDGER_INBOX_MODEL_BASE_URL=http://127.0.0.1:11434/v1
export LEDGER_INBOX_MODEL_API_KEY=local
export LEDGER_INBOX_MODEL_NAME=qwen3.5:4b
# Optional: a different vision model reads the original independently.
export LEDGER_INBOX_SHADOW_MODEL=qwen2.5vl:7b
uv run ledger-inbox worker
```

The endpoint must support image inputs and JSON Schema `response_format`.
There is no fallback to unconstrained text or a cloud endpoint on failure.
Remote endpoints require HTTPS; only local/Compose model connections permit HTTP.
For local Ollama's OpenAI-compatible endpoint, `reasoning_effort=none` disables
thinking where supported. The adapter resizes/converts decoded originals to PNG
in memory, so HEIC/AVIF ingestion does not depend on model-side codec support.

The shared versioned policy prioritizes actual paid amounts after discounts,
keeps amount/currency paired, distinguishes refunds and income, and demands
review for ambiguous group payments. Primary/shadow requests run sequentially;
the verifier receives pixels and the same policy, never the primary answer.
Disagreements, verifier failures, or uncertain verifier results stop automatic
writing. Raw answers and policy fingerprints are retained privately for audit.
These are safety gates, not a claimed accuracy score on all future screenshots.

## Email

```bash
uv run ledger-inbox create-alias --tenant-id TENANT_UUID --domain inbound.example.com
```

- `ledger-inbox imap` polls a dedicated TLS mailbox. Account identity, UIDVALIDITY,
  and UID determine message identity. Failed messages stay unread; later messages
  continue. Seen is set only after all accepted images are persisted.
- `POST /v1/inbound/email/rfc822` accepts HMAC-signed raw mail from a forwarding
  service. Message identity comes from signed bytes, not an unsigned override.

Email bodies are never passed to the recognizer. Actual mailbox/domain setup and
forwarder sender authentication remain operator tasks. See [email docs](docs/email.md).

## Ledger webhook contract

```bash
export LEDGER_INBOX_LEDGER_SINK=webhook
export LEDGER_INBOX_LEDGER_WEBHOOK_URL=https://ledger.example.com/write
export LEDGER_INBOX_LEDGER_WEBHOOK_SECRET=replace-me
# Optional query-only endpoint, separate from the write endpoint:
export LEDGER_INBOX_LEDGER_STATUS_URL=https://ledger.example.com/query
```

The receiver must enforce the stable `Idempotency-Key`, verify an optional HMAC,
and return a durable confirmation bound to that operation and exact payload.
Bare 2xx/202, timeouts, 5xx, redirects, and malformed/mismatched receipts remain
`delivery_unknown` and are never automatically re-dispatched. A read-only
reconciliation adapter can resolve them later. See [ledger protocol](docs/adapters.md).

## Configuration and Docker

Settings are environment variables; Python does **not** automatically load `.env`.
Start from [.env.example](.env.example). Docker Compose reads `.env` and maps the
supported variables into both services.

| Setting | Default | Meaning |
| --- | --- | --- |
| `LEDGER_INBOX_DATA_DIR` | `data` | private local SQLite, originals, directory ledger |
| `LEDGER_INBOX_MIN_CONFIDENCE` | `0.9` | automatic approval threshold |
| `LEDGER_INBOX_REQUIRED_FIELDS` | `occurred_at,category` | configurable non-money required fields |
| `LEDGER_INBOX_CATEGORIES` | empty | optional comma-separated category allowlist |
| `LEDGER_INBOX_SHADOW_MODEL` | empty | optional independent image verifier |
| `LEDGER_INBOX_MAX_IMAGE_BYTES` | `12582912` | per-image limit |
| `LEDGER_INBOX_MAX_IMAGE_PIXELS` | `25000000` | decoded image limit |
| `LEDGER_INBOX_MAX_EMAIL_BYTES` | `50331648` | total raw-message limit |
| `LEDGER_INBOX_LEASE_SECONDS` | `600` | recognition/dispatch lease |
| `LEDGER_INBOX_MAX_RECOGNITION_ATTEMPTS` | `3` | expired recognition attempt ceiling |

Amount must always be a positive integer in ISO 4217 minor units; currency and
transaction type cannot be omitted or guessed. Payment timestamps need timezone
information. Human approval may omit confidence but must still satisfy the
money/date/category rules. Configure required fields for your sink explicitly.
Foreign entries keep their original currency; this core does not perform FX or
assume every user's ledger uses CNY. Custom adapters can implement those policies.

```bash
docker compose up --build --wait
docker compose exec api ledger-inbox create-tenant --name "My ledger"
```

The service runs as UID 10001 with a private named volume. Compose exposes only
`127.0.0.1:8080`; an external reverse proxy must provide HTTPS, rate limits, and
request limits. Containers share the same configuration for review/ledger rules.
The core supports one configured sink for the installation; tenant isolation of
input/status is not a promise of separately configured destination accounts.

## Development and acceptance

```bash
uv sync --locked --extra dev
uv run ruff check src tests scripts examples
uv run ruff format --check src tests scripts examples
uv run pytest
uv run python scripts/smoke.py
uv build
```

The smoke test starts disposable real API/worker processes, generates a synthetic
image, exercises HTTP + signed email dedupe + review + one directory write +
tenant isolation + SQLite backup. It contacts no real mailbox, model, or financial
app. CI tests Python 3.11/3.13 and a fresh Docker Compose volume, including restart.
See [architecture](docs/architecture.md), [operations](docs/operations.md),
[clients](docs/clients.md), and [security](SECURITY.md).

## 中文简介

Ledger Inbox 是可扩展的截图记账服务：图片可以通过 HTTP 或邮件投递，识别模型、
分类规则和最终账本都可替换。0.2.0 补齐了人工复核 API/CLI、严格金额/币种校验、
任务中断恢复、邮件异常隔离，以及“写入结果不明时只对账、不重复写”的保护。

默认 mock 模型会把图片交给人工确认；启用视觉模型后可另配独立影子模型。
本项目不依赖 iCloud、MoneyThings 或 Hermes，外币默认保留原币；个人软件集成与
换汇规则通过适配器实现。当前仍为 Alpha，实际模型准确率、真实邮件供应商与具体
记账软件的接入效果需要在自己的环境验收。

## License

[Apache License 2.0](LICENSE)
