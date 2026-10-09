# Email ingestion

Email is a transport, not a trusted agent instruction channel. Ledger Inbox uses
the recipient alias only for tenant routing and consumes only image attachments.

## IMAP connector

Configure a dedicated mailbox or catch-all domain:

```bash
export LEDGER_INBOX_IMAP_HOST=imap.example.com
export LEDGER_INBOX_IMAP_USERNAME=ledger@example.com
export LEDGER_INBOX_IMAP_PASSWORD=app-password
uv run ledger-inbox imap --once
```

The connector uses TLS, searches for unread messages, fetches with `BODY.PEEK[]`,
and applies `\\Seen` only after all accepted attachments have been persisted. An
unknown recipient or message without an image is left unread for operator review.

For a long-running poller:

```bash
uv run ledger-inbox imap --poll-seconds 30
```

Use a least-privilege app password where the provider supports it. Do not use a
personal mailbox that contains unrelated mail.

## Signed RFC822 webhook

Set `LEDGER_INBOX_EMAIL_WEBHOOK_SECRET`, then send raw RFC822 bytes to:

```text
POST /v1/inbound/email/rfc822
Content-Type: message/rfc822
X-Ledger-Inbox-Recipient: li-random@inbound.example.com
X-Ledger-Inbox-Timestamp: 1786492800
X-Ledger-Inbox-Signature: sha256=<hex digest>
```

The signature input is:

```text
HMAC-SHA256(secret, timestamp + "." + lowercase_recipient + "." + raw_body)
```

Requests older than five minutes are rejected. The edge mail handler should also
verify provider-specific SPF/DKIM/DMARC results, rate-limit senders, and cap raw
message size before forwarding. Those signals are provider-specific and are not
invented by the generic endpoint.

## Alias guidance

- Generate unguessable aliases; do not expose sequential user IDs.
- Route multiple aliases to one mailbox or provider webhook.
- Revoke one alias when it leaks without rotating the tenant API key.
- Treat sender allowlists as an additional signal, not the only authentication
  boundary; sender headers can be forged before authenticated mail validation.

## Reliability and size limits (0.2)

The poller checks RFC822.SIZE before BODY.PEEK and rejects messages exceeding
`LEDGER_INBOX_MAX_EMAIL_BYTES`. The provider must also enforce a hard total-message
limit: the generic client cannot prevent a misreporting server from allocating
an oversized BODY response. Individual images must satisfy decoded byte/pixel limits.

Message identity includes a hash of host/account/mailbox, UIDVALIDITY and UID.
A mailbox rebuild or account switch therefore cannot silently reuse an old source
ID. A source ID reused with different bytes is a conflict. When ingestion or Seen
fails, the message remains unread, the error is recorded without secrets, and later
messages continue. Repeated failures update the same error record instead of
creating an unbounded log row per poll. Network failures back off to at most 5 minutes.

A message resolving to aliases from multiple tenants is not routed arbitrarily.
The shared mailbox's provider must preserve trusted delivery headers and apply
sender authentication; To/Cc alone are not proof of an authenticated sender.
Prefer the signed webhook for explicit recipient routing from a trusted edge.

The raw-mail endpoint applies its limit while reading, verifies timestamp/recipient/
body HMAC, then derives source identity from the signed raw bytes. An unsigned
`X-Ledger-Inbox-Message-ID` override is ignored. The timestamp must be within five
minutes in either direction; do not reuse a stale signature when forwarding retries.
