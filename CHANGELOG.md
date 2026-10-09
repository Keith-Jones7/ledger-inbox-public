# Changelog

## 0.2.0 — 2026-10-09 (Alpha)

Payment images can now complete a safe review-first workflow through the API or
CLI, and interrupted processing can resume without silently retrying ledger writes.

- Preserve explicit review flags; reject fractional/bool/overflow minor-unit
  amounts, missing/unsupported currencies, invalid times and weak confidence.
- Add refund semantics, configurable category/required-field rules, shared versioned
  payment policy and JSON Schema, and optional independent image-model verification.
- Decode real PNG/JPEG/WebP/HEIC/AVIF images; enforce byte/pixel/frame limits and
  convert model inputs to PNG in memory.
- Add tenant-scoped original-image viewing, approval/correction/ignore/retry,
  optimistic review versions, and before/after audit.
- Add recognition leases, bounded expiry retries, persistent pending operations,
  atomic dispatch claims, immutable payloads and guarded result transitions.
- Resolve unknown operations through read-only directory/webhook reconciliation;
  require operation/payload-bound webhook confirmation rather than bare status codes.
- Isolate bad IMAP messages, check size/Seen results, include account + UIDVALIDITY
  in identity, back off network failures and detect source-key/content conflicts.
- Hide storage paths and raw adapter errors from event APIs; bound multipart/raw
  mail bodies and create private files.
- Add additive schema migration, consistent SQLite backup, reproducible Docker
  dependencies/private volume permissions, process/container smoke tests and CI.

### Upgrade notes

Stop old processes and back up before migration. Webhook receivers must adopt the
new confirmation receipt; bare 2xx is now unknown. Undelivered 0.1 recognized entries
require review. See [operations](docs/operations.md). This release retains original
currencies and does not bundle personal Apple/Hermes or FX integrations.

## 0.1.0 — 2026-08-12

Initial multi-channel, tenant-scoped inbox with replaceable recognizer/ledger adapters.
