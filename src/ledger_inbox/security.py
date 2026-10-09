from __future__ import annotations

import hashlib
import hmac
import secrets
import time


def new_api_key() -> str:
    return "li_" + secrets.token_urlsafe(32)


def hash_api_key(api_key: str) -> str:
    return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


def new_inbound_alias(domain: str = "inbound.invalid") -> str:
    return f"li-{secrets.token_urlsafe(12).lower()}@{domain.lower()}"


def sign_webhook(secret: str, timestamp: str, recipient: str, body: bytes) -> str:
    message = timestamp.encode() + b"." + recipient.lower().encode() + b"." + body
    return "sha256=" + hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    secret: str,
    timestamp: str,
    recipient: str,
    body: bytes,
    signature: str,
    *,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError):
        return False
    current = int(time.time()) if now is None else now
    if abs(current - sent_at) > tolerance_seconds:
        return False
    expected = sign_webhook(secret, timestamp, recipient, body)
    return hmac.compare_digest(expected, signature)
