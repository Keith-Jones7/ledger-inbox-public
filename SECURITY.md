# Security policy

## Reporting a vulnerability

Please use GitHub's private vulnerability reporting for this repository. Do not
open a public issue containing exploit details, credentials, receipt images, or
financial data.

## Deployment boundary

Ledger Inbox is self-hosted software, not a managed security perimeter. Before
public exposure:

- terminate TLS with a trusted certificate;
- place the API behind rate limiting and request-size limits;
- keep the data directory private and encrypted at rest where appropriate;
- store API, IMAP, model, and webhook secrets outside source control;
- use a dedicated least-privilege mailbox;
- back up SQLite and images consistently and test restores;
- define image and event retention policies;
- restrict `/docs` if API discovery is sensitive for your deployment.

The generic email webhook authenticates the forwarding service with HMAC. The
forwarder remains responsible for provider-specific sender authentication and
abuse filtering. Email headers and image text are untrusted input.

No adapter should automatically retry a ledger write whose outcome is unknown.
Reconcile the operation ID against the destination first.

## 0.2 boundaries

Application controls include authenticated tenant-scoped image/review routes,
strict payment fields, bounded ingress streams, decoded-image limits, private
files, versioned reviews and immutable delivery operations. Tenant API keys are
bearer credentials and should be stored in a password manager/secret service.

The data directory includes sensitive original images, model responses, normalized
entries and review history. Network status responses project safe fields and
omit raw errors and filesystem references. Local CLI exports intentionally include
the original path for the operator and must not be copied into public issues.

Configured recognizers/sinks are trusted Python code. The installation uses one
sink/policy; configuring separate tenant ledger accounts is not provided by the
core. Remote endpoints require HTTPS, but an operator still chooses the destination
and controls data residency. Do not point a model or sink at an untrusted service.

Leases stop duplicate claims, not a live slow adapter's external side effects.
An expired dispatch becomes unknown and can only be reconciled. Custom adapters
must fit their configured lease and supply final confirmation proof.

The API/worker do not provide rate quotas, retention automation or a fully hosted
administration UI. Do not expose this Alpha installation without the deployment
controls above. Fresh Docker/process acceptance is separate from model accuracy,
mail provider acceptance and final accounting-app confirmation.
