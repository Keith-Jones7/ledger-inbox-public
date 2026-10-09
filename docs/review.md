# Review and reconciliation

All event API routes require `X-Ledger-Inbox-Key`. A tenant can only list, inspect,
view the original image, review, or reconcile its own events. CLI commands are
local operator tools: their access comes from ownership of the private data
store, and `--tenant-id` pins the selected tenant.

## API

- `GET /v1/events?status=needs_review` (also `recognition_failed`, `delivery_unknown`, etc.)
- `GET /v1/events/{id}` returns the normalized entry, status, current `version`, and safe audit summaries.
- `GET /v1/events/{id}/image` returns the verified original under the same authentication.
- `GET /v1/review/contract` exports current policy/schema and validation configuration.
- `POST /v1/events/{id}/review` accepts:

```json
{
  "expected_version": 3,
  "action": "approve",
  "entry": {
    "transaction_type": "expense",
    "amount_minor": 1299,
    "currency": "CNY",
    "merchant": "Example",
    "occurred_at": "2026-10-09T12:00:00+08:00",
    "category": "food",
    "needs_review": false,
    "review_reasons": []
  }
}
```

Actions `ignore` and `retry_recognition` omit `entry`. Approve requires the complete
corrected object. Clear model uncertainty only after inspecting the original;
invalid money, currency, type, dates or category rules still block approval.
Refund is a separate type; amounts remain positive. A confidence score is optional
for explicit human review.

Every transition checks event status/version and the absence of an existing
operation inside one SQLite transaction. A repeated/stale action returns 409.
Invalid entry returns 422 and changes neither version nor entry. Approval stores
before/after audit and makes the entry available to the worker; the API does not
perform the write itself. New processing versions cannot reuse an old approval.

Recognition failures are known to precede ledger dispatch and may be explicitly
retried after fixing the adapter. `delivery_unknown`, `delivery_failed`, active
writes, and completed entries cannot be reopened through ordinary review.

## CLI

```bash
ledger-inbox review list --tenant-id TENANT_UUID
ledger-inbox review list --tenant-id TENANT_UUID --status recognition_failed
ledger-inbox review export EVENT_ID --tenant-id TENANT_UUID
ledger-inbox review approve EVENT_ID --tenant-id TENANT_UUID \
  --expected-version VERSION --entry-file corrected-entry.json
ledger-inbox review ignore EVENT_ID --tenant-id TENANT_UUID --expected-version VERSION
ledger-inbox review retry EVENT_ID --tenant-id TENANT_UUID --expected-version VERSION
```

The local export includes an absolute original-image path so an operator can
inspect it. The network API never returns storage paths or raw adapter errors.

## Reconcile an ambiguous write

```bash
ledger-inbox review reconcile EVENT_ID --tenant-id TENANT_UUID
```

Or `POST /v1/events/{id}/reconcile`. Reconciliation uses the original sink identity,
operation ID and immutable payload. It never calls `deliver()`. Directory output
must match the full expected JSON. Webhook queries must return the confirmation
receipt specified in [adapters](adapters.md). A missing/conflicting response
leaves the event unknown. Active inflight operations must finish or expire before
reconciliation. A confirmed operation is idempotently returned as completed.

There is deliberately no command that blindly marks unknown entries successful
or retries them. A custom sink must supply a query adapter to prove its result.
