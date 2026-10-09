# Adapter development

Select a built-in adapter or `module.path:object`. Imported classes receive
`Settings`; imported instances are used directly. Adapters are trusted operator
extensions. Never perform ledger writes inside a recognizer.

## Recognizer

Implement `name` and `recognize(context) -> LedgerEntry`. Context includes the
local original image path and tenant/event identity. All image/email/metadata text
is untrusted input. Use `ledger_inbox.policy.policy_contract()` to share rules,
JSON Schema and fingerprints with automated/human reviewers.

```python
from ledger_inbox.domain import LedgerEntry

class MyRecognizer:
    name = "my-recognizer"
    def __init__(self, settings):
        self.settings = settings
    def recognize(self, context):
        return LedgerEntry(
            transaction_type="expense", amount_minor=1299, currency="CNY",
            merchant="Example", occurred_at="2026-10-09T12:00:00+08:00",
            category="food", confidence=0.98,
        )
```

The worker applies strict validation even to custom recognizers. Never truncate
amounts or fill unknown currencies. Keep `needs_review=True` for uncertainty,
regardless of whether a review reason is available. ISO minor units vary: JPY
has zero decimal places, CNY/USD two, KWD three. Refund is distinct from income.
Optional `last_audit` contains JSON-serializable raw responses/evidence; it remains
private in SQLite and is not exposed by event APIs.

The built-in adapter requires an OpenAI-compatible vision/JSON Schema endpoint.
Its independent verifier receives the original pixels and shared policy, never
primary model output. Calls run sequentially. Local Ollama compatibility uses
`reasoning_effort=none` ([official supported fields](https://docs.ollama.com/api/openai-compatibility)).

## Ledger sink

Implement `name` and `deliver(entry, *, event_id, operation_id) -> str`. Return a
nonempty durable external reference only after confirmed delivery. The worker
persists the immutable entry and stable ID before any call.

Raise `DefinitiveDeliveryError` only when the protocol proves no entry was created.
Raise `UnknownDeliveryError` for ambiguous effects. Unhandled adapter exceptions
are also unknown. Never automatically retry an ambiguous operation.

Optional `reconcile(entry, *, event_id, operation_id) -> str | None` is query-only:
return the confirmed reference after checking the exact immutable payload, or None.
It must never call a write operation. Without this method the CLI/API cannot
reconcile ambiguous custom deliveries.

## Webhook protocol (0.2)

The write endpoint receives UTF-8 canonical JSON:

```json
{"entry": {"amount_minor": 1299}, "event_id": "EVENT_UUID", "operation_id": "OPERATION_UUID"}
```

The actual `entry` contains every LedgerEntry field. Headers:

```text
Content-Type: application/json
Idempotency-Key: OPERATION_UUID
X-Ledger-Inbox-Signature: sha256=<HMAC-SHA256(secret, exact_request_bytes)>
```

The receiver must atomically retain/enforce operation ID and payload equality.
After durable ledger completion, return HTTP 200/201 with:

```json
{
  "state": "confirmed",
  "operation_id": "OPERATION_UUID",
  "payload_sha256": "SHA256_OF_CANONICAL_FULL_REQUEST",
  "external_ref": "stable-ledger-row-id"
}
```

Canonical request bytes use `json.dumps(payload, ensure_ascii=False, sort_keys=True,
separators=(",", ":"), allow_nan=False).encode("utf-8")`; helpers are
`operation_payload()` and `payload_hash()` in `ledger_inbox.adapters.ledgers`.

202, bare 200/204, redirects, 4xx/5xx, transport errors and mismatched receipts
remain unknown. An HTTP failure does not prove rollback. Configure a **separate
query-only** `LEDGER_INBOX_LEDGER_STATUS_URL` to receive signed POST
`{"operation_id":"OPERATION_UUID"}` and return the same receipt after checking
its stored payload. No confirmation means the operation stays unknown.

This protocol is stricter than 0.1's status-code-only success contract; upgrade
the receiver before enabling the new worker. Do not send pending writes to a new
destination without reconciling the old destination first.
