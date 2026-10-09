from __future__ import annotations

import hashlib
import hmac
import importlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

import httpx

from ..config import Settings, use_environment_proxy
from ..domain import LedgerEntry, LedgerSink
from ..image_store import private_directory, sync_directory


class DefinitiveDeliveryError(RuntimeError):
    """Only a protocol-level guarantee of no external write permits this result."""


class UnknownDeliveryError(RuntimeError):
    """The request may have reached the sink; never replay automatically."""


def operation_payload(entry: LedgerEntry, event_id: str, operation_id: str) -> dict:
    return {
        "operation_id": operation_id,
        "event_id": event_id,
        "entry": entry.to_dict(),
    }


def payload_hash(payload: dict) -> str:
    body = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return hashlib.sha256(body).hexdigest()


class DirectoryLedgerSink:
    name = "directory"

    def __init__(self, root: Path) -> None:
        self.root = root

    def deliver(self, entry: LedgerEntry, *, event_id: str, operation_id: str) -> str:
        private_directory(self.root)
        destination = self.root / f"{operation_id}.json"
        payload = operation_payload(entry, event_id, operation_id)
        body = (
            json.dumps(
                payload, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False
            )
            + "\n"
        ).encode()
        fd, temporary = tempfile.mkstemp(prefix=".entry-", dir=self.root)
        temp_path = Path(temporary)
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(body)
                output.flush()
                os.fsync(output.fileno())
            try:
                # No overwrite even when two clients race with the same operation ID.
                os.link(temp_path, destination)
                sync_directory(self.root)
            except FileExistsError:
                if destination.read_bytes() != body:
                    raise UnknownDeliveryError("operation_payload_conflict") from None
            return str(destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def reconcile(
        self, entry: LedgerEntry, *, event_id: str, operation_id: str
    ) -> str | None:
        destination = self.root / f"{operation_id}.json"
        if not destination.is_file():
            return None
        if json.loads(destination.read_bytes()) != operation_payload(
            entry, event_id, operation_id
        ):
            raise UnknownDeliveryError("operation_payload_conflict")
        return str(destination)


class WebhookLedgerSink:
    name = "webhook"

    def __init__(
        self, url: str, secret: str | None = None, status_url: str | None = None
    ) -> None:
        self.url, self.secret, self.status_url = url, secret, status_url

    def _headers(self, body: bytes, operation_id: str) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Idempotency-Key": operation_id}
        if self.secret:
            headers["X-Ledger-Inbox-Signature"] = (
                "sha256="
                + hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
            )
        return headers

    @staticmethod
    def _confirmation(response: httpx.Response, payload: dict) -> str | None:
        if response.status_code not in {200, 201}:
            return None
        try:
            result = response.json()
        except ValueError:
            return None
        if not isinstance(result, dict):
            return None
        reference = result.get("external_ref")
        if (
            result.get("state") != "confirmed"
            or result.get("operation_id") != payload["operation_id"]
            or result.get("payload_sha256") != payload_hash(payload)
        ):
            return None
        return (
            reference
            if isinstance(reference, str) and 0 < len(reference) <= 1024
            else None
        )

    def deliver(self, entry: LedgerEntry, *, event_id: str, operation_id: str) -> str:
        payload = operation_payload(entry, event_id, operation_id)
        body = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        try:
            response = httpx.post(
                self.url,
                content=body,
                headers=self._headers(body, operation_id),
                timeout=30,
                follow_redirects=False,
                trust_env=use_environment_proxy(self.url),
            )
        except httpx.RequestError:
            raise UnknownDeliveryError("ledger_transport_unknown") from None
        confirmation = self._confirmation(response, payload)
        if confirmation is None:
            raise UnknownDeliveryError("ledger_confirmation_missing")
        return confirmation

    def reconcile(
        self, entry: LedgerEntry, *, event_id: str, operation_id: str
    ) -> str | None:
        if not self.status_url:
            return None
        # Query-only endpoint. This is deliberately separate from the write URL.
        query = {"operation_id": operation_id}
        body = json.dumps(query, sort_keys=True, separators=(",", ":")).encode()
        response = httpx.post(
            self.status_url,
            content=body,
            headers=self._headers(body, operation_id),
            timeout=30,
            follow_redirects=False,
            trust_env=use_environment_proxy(self.status_url),
        )
        return self._confirmation(
            response, operation_payload(entry, event_id, operation_id)
        )


def load_ledger_sink(spec: str, settings: Settings) -> LedgerSink:
    if spec == "directory":
        return DirectoryLedgerSink(settings.data_dir / "ledger-outbox")
    if spec == "webhook":
        if not settings.ledger_webhook_url:
            raise ValueError("LEDGER_INBOX_LEDGER_WEBHOOK_URL is required")
        return WebhookLedgerSink(
            settings.ledger_webhook_url,
            settings.ledger_webhook_secret,
            settings.ledger_status_url,
        )
    loaded = _load_object(spec)
    instance = loaded(settings) if isinstance(loaded, type) else loaded
    if not hasattr(instance, "deliver") or not hasattr(instance, "name"):
        raise TypeError("ledger sink must expose name and deliver(entry, ...)")
    return instance


def _load_object(spec: str) -> Any:
    if ":" not in spec:
        raise ValueError("custom adapter must use module.path:object format")
    module, name = spec.split(":", 1)
    return getattr(importlib.import_module(module), name)
