from __future__ import annotations

from pathlib import Path

from ledger_inbox.adapters.ledgers import DirectoryLedgerSink, UnknownDeliveryError
from ledger_inbox.database import Database
from ledger_inbox.domain import LedgerEntry, RecognitionContext
from ledger_inbox.image_store import ImageStore
from ledger_inbox.ingestion import IngestionService
from ledger_inbox.security import new_api_key
from ledger_inbox.worker import Worker
from tests.helpers import image_bytes

PNG = image_bytes()


class CertainRecognizer:
    name = "certain-test"

    def recognize(self, context: RecognitionContext) -> LedgerEntry:
        return LedgerEntry(
            transaction_type="expense",
            amount_minor=1280,
            currency="CNY",
            merchant="Example Cafe",
            occurred_at="2026-08-12T12:00:00+08:00",
            category="food",
            confidence=0.99,
        )


class ReviewRecognizer:
    name = "review-test"

    def recognize(self, context: RecognitionContext) -> LedgerEntry:
        return LedgerEntry(
            transaction_type="unknown",
            amount_minor=None,
            currency="CNY",
            merchant=None,
            occurred_at=None,
            category=None,
            needs_review=True,
            review_reasons=("uncertain",),
        )


class UnknownSink:
    name = "unknown-test"

    def deliver(
        self, entry: LedgerEntry, *, event_id: str, operation_id: str
    ) -> str | None:
        raise UnknownDeliveryError("timeout after dispatch")


def queued_event(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    tenant = database.create_tenant("worker", new_api_key())
    service = IngestionService(database, ImageStore(tmp_path / "images"), 1024 * 1024)
    result = service.ingest_image(
        tenant_id=tenant["id"], image=PNG, filename="receipt.png", source_kind="https"
    )
    return database, result.event_id


def test_worker_persists_recognition_and_delivers_atomically_named_entry(
    tmp_path: Path,
) -> None:
    database, event_id = queued_event(tmp_path)
    worker = Worker(
        database, CertainRecognizer(), DirectoryLedgerSink(tmp_path / "outbox")
    )
    result = worker.run_once()
    event = database.get_event(event_id)

    assert result == {"event_id": event_id, "status": "completed"}
    assert event is not None
    assert event["status"] == "completed"
    assert event["recognition"]["entry"]["amount_minor"] == 1280
    assert Path(event["delivery"]["external_ref"]).is_file()


def test_worker_stops_for_review_without_delivering(tmp_path: Path) -> None:
    database, event_id = queued_event(tmp_path)
    worker = Worker(
        database, ReviewRecognizer(), DirectoryLedgerSink(tmp_path / "outbox")
    )
    result = worker.run_once()
    event = database.get_event(event_id)

    assert result == {"event_id": event_id, "status": "needs_review"}
    assert event is not None and event["status"] == "needs_review"
    assert "delivery" not in event


def test_unknown_delivery_is_never_requeued_automatically(tmp_path: Path) -> None:
    database, event_id = queued_event(tmp_path)
    worker = Worker(database, CertainRecognizer(), UnknownSink())
    first = worker.run_once()
    second = worker.run_once()
    event = database.get_event(event_id)

    assert first == {"event_id": event_id, "status": "delivery_unknown"}
    assert second is None
    assert event is not None and event["status"] == "delivery_unknown"
    assert event["delivery"]["state"] == "unknown"
