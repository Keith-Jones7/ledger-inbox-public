# Architecture and recovery contract

Ledger Inbox is a single-host API, optional IMAP poller and worker sharing private
SQLite/original-image storage. The target ledger is authoritative for delivered
entries. SQLite owns ingress, recognition, human-review audit and delivery state.

## Ingress

Images are fully decoded within byte/pixel limits, content-hashed and privately
stored/fsynced before an event is committed. Originals stay immutable.
`(tenant, content hash)` deduplicates across channels; every client source key is
also pinned to its original image, including keys first received as duplicate
content. A key reused for different content is a conflict, not a silent success.
Email webhook identity derives from authenticated raw bytes. IMAP identity includes
hashed host/account/mailbox identity, UIDVALIDITY and UID.

Each failed mail stays unread with a safe error record; later messages continue.
Mailbox Seen advances only after every accepted attachment is durable and the
server confirms the flag update. Retries after a partial ingestion are deduplicated.
IMAP fetch first checks RFC822.SIZE; deployments also need an upstream hard mailbox
size limit because a server can misreport size before a full BODY.PEEK response.

## Recognition and review

```text
queued -> processing -> needs_review -> human approve -> recognized
                    -> recognition_failed -> explicit retry -> queued
                    -> recognized
needs_review / recognition_failed -> human ignore -> ignored
recognized -> pending operation -> inflight -> completed
                                        -> delivery_unknown
                                        -> delivery_failed
```

Recognition claims have a random owner token and deadline. A live lease is never
stolen; after expiry the worker requeues up to the configured attempt ceiling,
then records recognition_failed. Old owners cannot persist results after expiry
or after another claim. A saved recognized entry survives interruption before
operation preparation; a saved pending operation survives interruption before
claim. No model rerun is necessary for these phases.

The portable policy/schema carries a version and fingerprint; the built-in model
adapter stores primary/shadow raw answers privately. Amount is strict integer
minor units; currency is validated against ISO 4217, with no missing-CNY fallback.
Explicit review requirements are monotonic through validation. Independent shadow
reads the image without primary output; uncertainty/failure/conflicts block writing.
This is a correctness guard, not an empirical guarantee about model accuracy.

Human review validates a complete corrected entry and uses version/status checks
under BEGIN IMMEDIATE. Before/after audit and state changes commit together.
Completed/ambiguous operations cannot be reopened by a stale category/action.

## Delivery

Preparation pins the exact approved entry and sink. New operation IDs are stable
UUID5 values derived from event ID; 0.1 existing UUIDs are retained. Only an atomic
pending→inflight claim permits deliver(). Other workers cannot claim the same
operation. The original payload is not mutated by later config or model changes.

Allowed outcomes:

| Current | Allowed next | Dispatch permitted? |
| --- | --- | --- |
| pending | inflight | only the atomic claimant |
| inflight | confirmed, failed, unknown | no additional dispatch |
| unknown | confirmed after proof | never |
| confirmed | confirmed | never |
| failed | failed | never through automated retry |

An expired inflight lease becomes unknown, even when the process may have died
before sending. Late successful results from the original claimant can confirm
it; late failures cannot downgrade confirmed state. Operation and event changes
commit/roll back together. Results return persisted winner state.

Directory writes use exclusive linking and directory fsync: no race may overwrite
an existing operation file. Webhooks require an operation/payload-bound receipt;
status codes alone never establish ledger completion. Query adapters can reconcile
unknown outcomes without calling the write path.

## Isolation and scope

Tenant API keys/aliases, images, events, dedupe and review/reconciliation are scoped.
The installation currently has one configured ledger sink and category policy;
custom destination routing is an adapter responsibility. This is not a hosted
multi-account accounting platform. Queue images are not automatically deleted;
retention and encrypted backups remain operator policy.

0.2 deliberately does not bundle Apple OCR, personal merchant-learning rules, FX
conversion, MoneyThings or Hermes. These can be optional adapters; the core remains
portable and retains original-currency entries.
