from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from .adapters.ledgers import DefinitiveDeliveryError
from .config import Settings
from .database import Database, StateConflict
from .domain import LedgerEntry, LedgerSink, RecognitionContext, Recognizer
from .policy import policy_contract


class Worker:
    def __init__(
        self,
        database: Database,
        recognizer: Recognizer,
        ledger_sink: LedgerSink,
        settings: Settings | None = None,
    ) -> None:
        self.database, self.recognizer, self.ledger_sink = (
            database,
            recognizer,
            ledger_sink,
        )
        self.settings = settings or Settings()

    def _dispatch(self) -> dict | None:
        operation = self.database.claim_delivery(
            self.ledger_sink.name, self.settings.lease_seconds
        )
        if operation is None:
            return None
        # Frozen payload is the exact approved object; re-validation must not mutate it.
        entry = LedgerEntry(
            **{
                **json.loads(operation["payload_json"]),
                "review_reasons": tuple(
                    json.loads(operation["payload_json"]).get("review_reasons", [])
                ),
            }
        )
        try:
            reference = self.ledger_sink.deliver(
                entry, event_id=operation["event_id"], operation_id=operation["id"]
            )
            if not isinstance(reference, str) or not reference:
                state, error = "unknown", "ledger_confirmation_missing"
            else:
                state, error = "confirmed", None
        except DefinitiveDeliveryError:
            state, reference, error = "failed", None, "ledger_rejected_without_write"
        except Exception:
            state, reference, error = "unknown", None, "ledger_delivery_unknown"
        status = self.database.finish_delivery(
            operation["event_id"],
            operation["id"],
            state=state,
            external_ref=reference,
            error=error,
            dispatch_token=operation["dispatch_token"],
        )
        return {"event_id": operation["event_id"], "status": status}

    def run_once(self) -> dict | None:
        self.database.recover_expired(self.settings.max_recognition_attempts)
        event_id = self.database.next_recognized()
        if event_id:
            self.database.prepare_delivery(event_id, self.ledger_sink.name)
        result = self._dispatch()
        if result:
            return result
        event = self.database.claim_next_event(self.settings.lease_seconds)
        if event is None:
            return None
        context = RecognitionContext(
            event["id"],
            event["tenant_id"],
            event["image_path"],
            event["media_type"],
            event["filename"],
            event["captured_at"],
            event["metadata"],
        )
        try:
            if (
                hashlib.sha256(Path(context.image_path).read_bytes()).hexdigest()
                != event["content_sha256"]
            ):
                raise ValueError("stored image integrity failure")
            entry = self.recognizer.recognize(context).validated(
                **self.settings.validation
            )
        except Exception:
            self.database.fail_recognition(
                event["id"], "recognizer_failed", lease_token=event["lease_token"]
            )
            return {"event_id": event["id"], "status": "recognition_failed"}
        audit = {
            "policy": policy_contract(self.settings.categories),
            **getattr(self.recognizer, "last_audit", {}),
        }
        self.database.save_recognition(
            event["id"],
            self.recognizer.name,
            entry.to_dict(),
            entry.needs_review,
            lease_token=event["lease_token"],
            audit=audit,
        )
        if entry.needs_review:
            return {"event_id": event["id"], "status": "needs_review"}
        self.database.prepare_delivery(event["id"], self.ledger_sink.name)
        return self._dispatch() or {"event_id": event["id"], "status": "recognized"}

    def reconcile(self, event_id: str, tenant_id: str) -> dict:
        self.database.recover_expired(self.settings.max_recognition_attempts)
        event = self.database.get_event(event_id, tenant_id)
        if event is None:
            raise KeyError("event not found")
        operation = event.get("delivery")
        if operation is None or operation["state"] not in {"unknown", "confirmed"}:
            raise StateConflict("operation is not ready for reconciliation")
        if operation["sink"] != self.ledger_sink.name:
            raise StateConflict("configured sink does not match the pinned operation")
        if operation["state"] == "confirmed":
            return {"event_id": event_id, "status": event["status"]}
        if not hasattr(self.ledger_sink, "reconcile"):
            raise StateConflict("sink has no read-only reconciliation adapter")
        payload = json.loads(operation["payload_json"])
        entry = LedgerEntry(
            **{**payload, "review_reasons": tuple(payload.get("review_reasons", []))}
        )
        try:
            reference = self.ledger_sink.reconcile(
                entry, event_id=event_id, operation_id=operation["id"]
            )
        except Exception:
            reference = None
        if isinstance(reference, str) and reference:
            self.database.confirm_reconciliation(event_id, operation["id"], reference)
        return {
            "event_id": event_id,
            "status": self.database.get_event(event_id, tenant_id)["status"],
        }

    def run_forever(self, poll_seconds: float = 2.0) -> None:
        while True:
            try:
                result = self.run_once()
            except (StateConflict, OSError, sqlite3.OperationalError):
                result = None
            if result is None:
                time.sleep(poll_seconds)
