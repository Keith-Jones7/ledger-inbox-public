from __future__ import annotations

import hashlib
import imaplib
import logging
import re
import ssl
import time
from dataclasses import dataclass

from .database import Database
from .ingestion import IngestionService, recipients_from_message

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IMAPSettings:
    host: str
    username: str
    password: str
    mailbox: str = "INBOX"
    port: int = 993
    timeout_seconds: int = 30


class IMAPConnector:
    def __init__(
        self, config: IMAPSettings, database: Database, ingestion: IngestionService
    ) -> None:
        self.config, self.database, self.ingestion = config, database, ingestion

    def poll_once(self) -> dict[str, int]:
        accepted, skipped = 0, 0
        with imaplib.IMAP4_SSL(
            self.config.host,
            self.config.port,
            ssl_context=ssl.create_default_context(),
            timeout=self.config.timeout_seconds,
        ) as client:
            client.login(self.config.username, self.config.password)
            status, _ = client.select(self.config.mailbox)
            if status != "OK":
                raise RuntimeError("imap_mailbox_unavailable")
            _, validity = client.response("UIDVALIDITY")
            if not validity or not validity[0] or not validity[0].isdigit():
                raise RuntimeError("imap_uidvalidity_unavailable")
            account = hashlib.sha256(
                f"{self.config.host}:{self.config.username}:{self.config.mailbox}".encode()
            ).hexdigest()
            status, payload = client.uid("search", None, "UNSEEN")
            if status != "OK":
                raise RuntimeError("imap_search_failed")
            uids = payload[0].split() if payload and payload[0] else []
            for uid in uids:
                source_id = f"imap:{account}:{validity[0].decode()}:{uid.decode()}"
                try:
                    status, size_response = client.uid("fetch", uid, "(RFC822.SIZE)")
                    size = _message_size(size_response) if status == "OK" else None
                    if size is None or size > self.ingestion.max_email_bytes:
                        raise ValueError("imap_message_size_rejected")
                    status, fetched = client.uid("fetch", uid, "(BODY.PEEK[])")
                    raw = _extract_message(fetched) if status == "OK" else None
                    if raw is None or len(raw) > self.ingestion.max_email_bytes:
                        raise ValueError("imap_message_unavailable")
                    recipient, tenant = self._resolve_recipient(raw)
                    if tenant is None:
                        raise ValueError("imap_recipient_unresolved")
                    self.ingestion.ingest_email(
                        tenant_id=tenant["id"],
                        raw_message=raw,
                        source_event_id=source_id,
                        recipient=recipient,
                    )
                    status, _ = client.uid("store", uid, "+FLAGS", "(\\Seen)")
                    if status != "OK":
                        raise ValueError("imap_seen_update_failed")
                    accepted += 1
                except (ValueError, imaplib.IMAP4.error, OSError):
                    # Retain unread mail, persist a safe error and keep later messages moving.
                    self.database.record_ingress_error(
                        "imap", source_id, "imap_message_skipped"
                    )
                    skipped += 1
        return {"accepted_messages": accepted, "skipped_messages": skipped}

    def run_forever(self, poll_seconds: float = 30.0) -> None:
        failures = 0
        while True:
            try:
                self.poll_once()
                failures = 0
            except (imaplib.IMAP4.error, OSError, RuntimeError):
                failures = min(failures + 1, 5)
                logger.warning(
                    "IMAP poll unavailable; retrying without advancing mailbox state"
                )
            time.sleep(min(300, poll_seconds * 2**failures))

    def _resolve_recipient(self, raw: bytes) -> tuple[str | None, dict | None]:
        matches = {}
        for recipient in recipients_from_message(raw):
            tenant = self.database.tenant_for_alias(recipient)
            if tenant:
                matches[tenant["id"]] = (recipient, tenant)
        # Never pick an arbitrary tenant from a message mentioning multiple aliases.
        return next(iter(matches.values())) if len(matches) == 1 else (None, None)


def _message_size(response: list) -> int | None:
    for part in response:
        if isinstance(part, bytes):
            matched = re.search(rb"RFC822.SIZE\s+(\d+)", part)
            if matched:
                return int(matched[1])
    return None


def _extract_message(response: list) -> bytes | None:
    for part in response:
        if isinstance(part, tuple) and len(part) >= 2 and isinstance(part[1], bytes):
            return part[1]
    return None
