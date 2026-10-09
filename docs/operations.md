# Operations, upgrade and acceptance

## Upgrade from 0.1

1. Stop API, worker and IMAP processes. Do not mix old/new workers during migration.
2. Back up the local SQLite store consistently and archive its immutable images
   and directory ledger, plus secret configuration separately outside Git.
3. Install 0.2 with `uv sync --locked --extra dev` or rebuild the Docker image.
4. `ledger-inbox init` applies additive schema migration under one transaction.
   Existing event IDs, image hashes, operation IDs and confirmations remain intact.
   Old undelivered recognized entries require fresh review under the stricter policy.
5. Upgrade webhook receivers to the operation/payload-bound receipt before enabling
   writes. Old inflight operations expire into unknown and need reconciliation.
6. Start API/worker/poller, inspect needs_review/recognition_failed/delivery_unknown,
   and exercise one authorized real input in your environment.

The schema version is 2. A future/newer schema is rejected instead of downgraded.
Do not downgrade the executable and run it against a migrated store.

## Deployment

Docker Compose uses a named volume owned by UID 10001; bind mounts must be prepared
with equivalent private ownership/permissions. API and worker share the volume.
Only loopback port 8080 is published. A trusted reverse proxy is needed for external
HTTPS, rate limiting and whole-request limits (multipart overhead included).
HTTP uploads are bounded before multipart spooling; emails are bounded while
reading. Upstream IMAP quotas/message-size limits are still required.

The worker lease must exceed the longest complete recognition/dispatch call. The
built-in sequential two-model deadlines fit the default 600s lease. Custom adapters
must also finish within the lease. A too-slow writer is conservative: lease expiry
means unknown, never permission to write again.

Changing model/category policy only affects new recognition/review. Pending
operation payloads are immutable. Sink name pins operations; if you change URL or
credentials for an existing sink, reconcile pending/unknown operations against the
original destination first. Configuration is not a migration tool.

Keep data/SQLite/images/output private. Do not put an active database in a cloud
sync folder or on shared multi-host network storage. Keys are configured outside
source control. Rotate/revoke tenant keys and aliases through local operator
administration until a dedicated management API is provided.

## Backup and restore

```bash
ledger-inbox backup --destination /private/backup/new-snapshot.sqlite3
```

SQLite's backup API creates a consistent, private snapshot and refuses to overwrite
an existing destination. **This command backs up SQLite only.** Stop writers and
copy immutable `images/` and `ledger-outbox/`, plus deployment/secret configuration,
into the same backup set. Retain the directory layout and verify image hashes.
Encryption, retention and off-host storage are operator responsibilities.

Before disaster restore, stop/retire the old instance, restore all files into a
private local directory, run integrity checks and migration, then reconcile
operations against the destination. A historical backup may predate external
writes: do not simply start a worker against it. Compare/recover the latest operation
history first; unknown/possibly delivered work must not be blindly re-dispatched.
A stale backup's queued events also need this audit. No automated disaster-restore
claim is made by the SQLite snapshot smoke test.

## Automated checks

`uv run pytest` exercises real temporary SQLite transactions, independent claims,
rollback triggers, expired leases, stale approvals, corrupted inputs, IMAP UID
reuse/poison messages and exact webhook receipts. Transport adapters are simulated;
those tests do not prove a real model's accuracy or a real accounting app's effect.

`uv run python scripts/smoke.py` launches actual API/worker processes against a
fresh temporary root and a directory sink. CI additionally starts a fresh Compose
volume and repeats the smoke after service restart. All images and credentials
used by this acceptance flow are synthetic; no personal financial data is required.

## Release boundary

0.2 remains Alpha. A deployer must independently validate their model, mailbox
provider, TLS/proxy and real target ledger, including one unique final operation
and correct money/direction/time. Merchant learning, deterministic FX and
MoneyThings/Hermes integrations are optional adapter work, not capabilities claimed
by this release. A successful build or an HTTP acceptance is not final ledger proof.
