from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any

from .database import Database
from .domain import valid_timestamp
from .image_store import ImageStore


@dataclass(frozen=True)
class IngestResult:
    event_id: str
    status: str
    duplicate: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "status": self.status,
            "duplicate": self.duplicate,
        }


class IngestionService:
    def __init__(
        self,
        database: Database,
        store: ImageStore,
        max_image_bytes: int,
        max_email_bytes: int = 48 * 1024 * 1024,
    ) -> None:
        self.database = database
        self.store = store
        self.max_image_bytes = max_image_bytes
        self.max_email_bytes = max_email_bytes

    def ingest_image(
        self,
        *,
        tenant_id: str,
        image: bytes,
        filename: str,
        source_kind: str,
        source_event_id: str | None = None,
        captured_at: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IngestResult:
        if captured_at is not None and not valid_timestamp(captured_at):
            raise ValueError("capture timestamp requires an ISO datetime with timezone")
        if source_event_id is not None and (
            not source_event_id.strip() or len(source_event_id) > 512
        ):
            raise ValueError("invalid source event ID")
        if not image:
            raise ValueError("image is empty")
        if len(image) > self.max_image_bytes:
            raise ValueError(f"image exceeds {self.max_image_bytes} byte limit")
        digest, media_type, path = self.store.put(tenant_id, image)
        event, duplicate = self.database.create_event(
            tenant_id=tenant_id,
            source_kind=source_kind,
            source_event_id=source_event_id,
            digest=digest,
            filename=_safe_filename(filename),
            media_type=media_type,
            image_path=path,
            captured_at=captured_at,
            metadata=metadata or {},
        )
        return IngestResult(
            event_id=event["id"], status=event["status"], duplicate=duplicate
        )

    def ingest_email(
        self,
        *,
        tenant_id: str,
        raw_message: bytes,
        source_event_id: str | None = None,
        recipient: str | None = None,
    ) -> list[IngestResult]:
        if len(raw_message) > self.max_email_bytes:
            raise ValueError("email exceeds byte limit")
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        message_id = source_event_id or hashlib.sha256(raw_message).hexdigest()
        results: list[IngestResult] = []
        for index, (filename, payload) in enumerate(_image_attachments(message)):
            results.append(
                self.ingest_image(
                    tenant_id=tenant_id,
                    image=payload,
                    filename=filename,
                    source_kind="email",
                    source_event_id=f"{message_id}:{index}",
                    metadata={
                        "email_message_id": _message_id(message),
                        "email_recipient": recipient,
                        "attachment_index": index,
                    },
                )
            )
        if not results:
            raise ValueError("email contains no supported image attachment")
        return results


def recipients_from_message(raw_message: bytes) -> list[str]:
    message = BytesParser(policy=policy.default).parsebytes(raw_message)
    values: list[str] = []
    for header in ("to", "cc", "delivered-to", "x-original-to"):
        values.extend(message.get_all(header, []))
    return [address.lower() for _, address in getaddresses(values) if address]


def _image_attachments(message: Message) -> Iterable[tuple[str, bytes]]:
    for index, part in enumerate(message.walk()):
        if part.is_multipart():
            continue
        content_type = part.get_content_type().lower()
        disposition = part.get_content_disposition()
        filename = part.get_filename()
        if not content_type.startswith("image/"):
            continue
        if disposition not in {"attachment", "inline", None}:
            continue
        payload = part.get_payload(decode=True)
        if payload:
            yield filename or f"attachment-{index}", payload


def _message_id(message: Message) -> str | None:
    value = str(message.get("Message-ID") or "").strip()
    return value or None


def _safe_filename(filename: str) -> str:
    name = filename.replace("\\", "/").rsplit("/", 1)[-1].strip().replace("\x00", "")
    return name[:255] or "image"
