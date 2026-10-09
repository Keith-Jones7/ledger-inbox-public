# Client integration

Clients only need an HTTPS endpoint and tenant API key. They do not need to know
which OCR, model, or ledger runs behind the service.

## iPhone Shortcut

A minimal Shortcut can:

1. Receive a screenshot from the Share Sheet or run after **Take Screenshot**.
2. Generate a UUID for this submission.
3. Use **Get Contents of URL**:
   - URL: `https://your-host.example/v1/ingest/images`
   - Method: `POST`
   - Request Body: `Form`
   - field `image`: the screenshot file
   - header `X-Ledger-Inbox-Key`: the tenant API key
   - header `Idempotency-Key`: the generated UUID
   - optional header `X-Captured-At`: an ISO 8601 timestamp
4. Show a short notification containing the returned status.

Store the API key in the Shortcut configuration and never include it in the URL.
Use normal HTTPS with a trusted certificate. A side-button action can invoke the
Shortcut, but iOS setup varies by device and OS version.

## Email Shortcut

For the lowest-friction client, the Shortcut can email the screenshot as an
attachment to its random inbound alias. Keep the subject/body empty or static;
the server ignores them. Email delivery is asynchronous, so clients should not
present “booked” until a later status or review notification proves the result.

## HTTP contract

Successful ingestion returns HTTP `202`:

```json
{
  "event_id": "b19af91d-...",
  "status": "queued",
  "duplicate": false
}
```

Resubmitting the same image for the same tenant returns the existing event with
`duplicate: true`. Clients should reuse a stable `Idempotency-Key` for retries.

The capture timestamp must include a timezone, e.g. `2026-10-09T12:00:00+08:00`.
It is capture metadata, not proof of payment time. Version 0.2 rejects a reused
source key with different image bytes (409). Valid images are actually decoded;
a file extension or a PNG-like header alone is insufficient.

Poll `GET /v1/events/{id}` under the same API key for the final status. `queued`,
`recognized`, `delivering`, and email/HTTP 202 mean accepted or in progress.
Only `completed` means the configured sink has supplied its required completion
proof. `needs_review` and `recognition_failed` have the [review workflow](review.md);
`delivery_unknown` requires query-only reconciliation. The event API hides storage
paths and adapter error details. GET `/v1/events/{id}/image` uses identical tenant
authentication and returns the verified original with no-store caching.
