from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ledger_inbox.adapters.ledgers import DirectoryLedgerSink, UnknownDeliveryError
from ledger_inbox.api import create_app
from ledger_inbox.config import Settings
from ledger_inbox.database import Database, StateConflict
from ledger_inbox.domain import LedgerEntry
from ledger_inbox.image_store import ImageStore
from ledger_inbox.ingestion import IngestionService
from ledger_inbox.worker import Worker
from tests.helpers import image_bytes

ENTRY = LedgerEntry(
    "expense",
    1280,
    "CNY",
    "Example",
    "2026-10-09T12:00:00+08:00",
    "food",
    confidence=0.99,
)


class Recognizer:
    name = "fixture"

    def __init__(self, entry=ENTRY):
        self.entry = entry
        self.calls = 0

    def recognize(self, context):
        self.calls += 1
        return self.entry


@pytest.fixture
def fixture(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    tenant = db.create_tenant("fixture", "fixture-key")
    service = IngestionService(db, ImageStore(tmp_path / "images"), 1024 * 1024)
    event = service.ingest_image(
        tenant_id=tenant["id"],
        image=image_bytes(),
        filename="fixture.png",
        source_kind="https",
    )
    return db, tenant, service, event.event_id


def expire(db, table):
    with db.transaction() as connection:
        connection.execute(
            f"UPDATE {table} SET lease_until='2000-01-01T00:00:00+00:00'"
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"amount_minor": 123.9},
        {"amount_minor": True},
        {"amount_minor": "100"},
        {"amount_minor": 2**63},
        {"currency": None},
        {"currency": "ZZZ"},
        {"currency": "XXX"},
        {"confidence": 0.01},
        {"confidence": float("nan")},
        {"occurred_at": "yesterday"},
        {"occurred_at": "2026-10-09T12:00:00"},
        {"category": None},
        {"transaction_type": "other"},
    ],
)
def test_invalid_core_fields_cannot_pass(updates):
    assert LedgerEntry.from_dict({**ENTRY.to_dict(), **updates}).needs_review


def test_explicit_review_is_never_cleared(fixture, tmp_path):
    db, _, _, event_id = fixture
    result = Worker(
        db,
        Recognizer(replace(ENTRY, needs_review=True)),
        DirectoryLedgerSink(tmp_path / "out"),
    ).run_once()
    assert result["status"] == "needs_review"
    assert "delivery" not in db.get_event(event_id)
    assert not (tmp_path / "out").exists()


def test_refund_and_currency_minor_units_are_explicit():
    assert (
        not replace(ENTRY, transaction_type="refund", currency="JPY", amount_minor=1280)
        .validated()
        .needs_review
    )


def test_live_recognition_lease_is_not_stolen(fixture, tmp_path):
    db, _, _, event_id = fixture
    claimed = db.claim_next_event()
    worker = Worker(db, Recognizer(), DirectoryLedgerSink(tmp_path / "out"))
    assert worker.run_once() is None
    assert db.get_event(event_id)["lease_token"] == claimed["lease_token"]


def test_expired_recognition_resumes_and_old_owner_is_rejected(fixture, tmp_path):
    db, _, _, event_id = fixture
    claimed = db.claim_next_event()
    expire(db, "ingest_events")
    assert (
        Worker(db, Recognizer(), DirectoryLedgerSink(tmp_path / "out")).run_once()[
            "status"
        ]
        == "completed"
    )
    with pytest.raises(StateConflict):
        db.save_recognition(
            event_id, "old", ENTRY.to_dict(), False, lease_token=claimed["lease_token"]
        )
    assert db.get_event(event_id)["status"] == "completed"


def test_repeated_expired_recognition_stops_at_attempt_budget(fixture):
    db, _, _, event_id = fixture
    for _ in range(3):
        db.claim_next_event()
        expire(db, "ingest_events")
        db.recover_expired()
    assert db.get_event(event_id)["status"] == "recognition_failed"
    assert db.claim_next_event() is None


def recognized(db, event_id):
    claim = db.claim_next_event()
    db.save_recognition(
        event_id, "fixture", ENTRY.to_dict(), False, lease_token=claim["lease_token"]
    )


@pytest.mark.parametrize("prepare", [False, True])
def test_recognized_and_pending_operations_resume_without_model_replay(
    fixture, tmp_path, prepare
):
    db, _, _, event_id = fixture
    recognized(db, event_id)
    if prepare:
        db.prepare_delivery(event_id, "directory")
    recognizer = Recognizer()
    assert (
        Worker(db, recognizer, DirectoryLedgerSink(tmp_path / "out")).run_once()[
            "status"
        ]
        == "completed"
    )
    assert recognizer.calls == 0


def test_atomic_delivery_claim_prevents_two_dispatchers(fixture):
    db, _, _, event_id = fixture
    recognized(db, event_id)
    operation_id = db.prepare_delivery(event_id, "directory")
    second = Database(db.path)
    first = db.claim_delivery("directory")
    assert first["id"] == operation_id
    assert second.claim_delivery("directory") is None


def test_stale_result_cannot_downgrade_confirmed_state(fixture):
    db, _, _, event_id = fixture
    recognized(db, event_id)
    op_id = db.prepare_delivery(event_id, "directory")
    claim = db.claim_delivery("directory")
    db.finish_delivery(
        event_id,
        op_id,
        state="confirmed",
        external_ref="fixture",
        dispatch_token=claim["dispatch_token"],
    )
    assert (
        db.finish_delivery(
            event_id,
            op_id,
            state="unknown",
            error="stale",
            dispatch_token=claim["dispatch_token"],
        )
        == "completed"
    )
    event = db.get_event(event_id)
    assert event["delivery"]["state"] == "confirmed"
    assert event["last_error"] is None


def test_delivery_and_event_update_roll_back_together(fixture):
    db, _, _, event_id = fixture
    recognized(db, event_id)
    op_id = db.prepare_delivery(event_id, "directory")
    claim = db.claim_delivery("directory")
    with db.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_status BEFORE UPDATE ON ingest_events WHEN NEW.status='completed' BEGIN SELECT RAISE(ABORT,'fixture abort'); END"
        )
    with pytest.raises(sqlite3.IntegrityError):
        db.finish_delivery(
            event_id,
            op_id,
            state="confirmed",
            external_ref="fixture",
            dispatch_token=claim["dispatch_token"],
        )
    event = db.get_event(event_id)
    assert event["status"] == "delivering"
    assert event["delivery"]["state"] == "inflight"


def test_inflight_crash_becomes_unknown_and_never_dispatches(fixture, tmp_path):
    db, _, _, event_id = fixture
    recognized(db, event_id)
    db.prepare_delivery(event_id, "directory")
    claim = db.claim_delivery("directory")
    expire(db, "delivery_operations")
    assert (
        Worker(db, Recognizer(), DirectoryLedgerSink(tmp_path / "out")).run_once()
        is None
    )
    assert db.get_event(event_id)["delivery"]["state"] == "unknown"
    # A delayed confirmation from the original owner may safely resolve lease expiry.
    assert (
        db.finish_delivery(
            event_id,
            claim["id"],
            state="confirmed",
            external_ref="fixture",
            dispatch_token=claim["dispatch_token"],
        )
        == "completed"
    )


def test_late_directory_confirmation_is_reconciled_without_replay(fixture, tmp_path):
    db, tenant, _, event_id = fixture

    class DelayedDirectory(DirectoryLedgerSink):
        calls = 0

        def deliver(self, *args, **kwargs):
            self.calls += 1
            super().deliver(*args, **kwargs)
            raise UnknownDeliveryError("fixture lost response")

    sink = DelayedDirectory(tmp_path / "out")
    worker = Worker(db, Recognizer(), sink)
    assert worker.run_once()["status"] == "delivery_unknown"
    assert worker.run_once() is None
    assert worker.reconcile(event_id, tenant["id"])["status"] == "completed"
    assert worker.run_once() is None
    assert sink.calls == 1


def test_conflicting_directory_file_cannot_be_confirmed(fixture, tmp_path):
    db, tenant, _, event_id = fixture
    recognized(db, event_id)
    op_id = db.prepare_delivery(event_id, "directory")
    claim = db.claim_delivery("directory")
    db.finish_delivery(
        event_id, op_id, state="unknown", dispatch_token=claim["dispatch_token"]
    )
    out = tmp_path / "out"
    out.mkdir()
    (out / f"{op_id}.json").write_text("{}")
    worker = Worker(db, Recognizer(), DirectoryLedgerSink(out))
    assert worker.reconcile(event_id, tenant["id"])["status"] == "delivery_unknown"


def test_directory_sink_never_overwrites_conflicting_operations(tmp_path):
    sink = DirectoryLedgerSink(tmp_path / "out")
    ref = sink.deliver(ENTRY, event_id="fixture", operation_id="operation")
    before = Path(ref).read_bytes()
    with pytest.raises(UnknownDeliveryError):
        sink.deliver(
            replace(ENTRY, amount_minor=1), event_id="fixture", operation_id="operation"
        )
    assert Path(ref).read_bytes() == before


def test_api_review_closes_default_mock_flow_and_rejects_replay(fixture, tmp_path):
    db, tenant, _, event_id = fixture
    worker = Worker(
        db,
        Recognizer(replace(ENTRY, needs_review=True, review_reasons=("uncertain",))),
        DirectoryLedgerSink(tmp_path / "out"),
    )
    worker.run_once()
    client = TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path)))
    headers = {"X-Ledger-Inbox-Key": "fixture-key"}
    event = client.get(f"/v1/events/{event_id}", headers=headers).json()
    approved = {**ENTRY.to_dict(), "confidence": None}
    request = {
        "expected_version": event["version"],
        "action": "approve",
        "entry": approved,
    }
    response = client.post(
        f"/v1/events/{event_id}/review", headers=headers, json=request
    )
    assert response.status_code == 200
    assert response.json()["status"] == "recognized"
    assert (
        client.post(
            f"/v1/events/{event_id}/review", headers=headers, json=request
        ).status_code
        == 409
    )
    assert worker.run_once()["status"] == "completed"
    assert len(db.get_event(event_id)["reviews"]) == 1
    assert (
        db.get_event(event_id, tenant["id"])["recognition"]["entry"]["confidence"]
        is None
    )


def test_api_invalid_approval_is_atomic(fixture, tmp_path):
    db, _, _, event_id = fixture
    Worker(
        db,
        Recognizer(replace(ENTRY, needs_review=True)),
        DirectoryLedgerSink(tmp_path / "out"),
    ).run_once()
    client = TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path)))
    before = db.get_event(event_id)
    response = client.post(
        f"/v1/events/{event_id}/review",
        headers={"X-Ledger-Inbox-Key": "fixture-key"},
        json={
            "expected_version": before["version"],
            "action": "approve",
            "entry": {**ENTRY.to_dict(), "amount_minor": 3.9},
        },
    )
    assert response.status_code == 422
    assert db.get_event(event_id)["version"] == before["version"]
    assert "delivery" not in db.get_event(event_id)


def test_all_event_routes_isolate_other_tenants(fixture, tmp_path):
    db, _, _, event_id = fixture
    db.create_tenant("other", "other-key")
    client = TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path)))
    headers = {"X-Ledger-Inbox-Key": "other-key"}
    for suffix in ("", "/image"):
        assert (
            client.get(f"/v1/events/{event_id}{suffix}", headers=headers).status_code
            == 404
        )
    assert (
        client.post(
            f"/v1/events/{event_id}/review",
            headers=headers,
            json={"expected_version": 1, "action": "ignore"},
        ).status_code
        == 404
    )
    assert (
        client.post(f"/v1/events/{event_id}/reconcile", headers=headers).status_code
        == 404
    )
    assert (
        client.get("/v1/events?status=queued", headers=headers).json()["events"] == []
    )


def test_api_hides_paths_and_adapter_error_details(fixture, tmp_path):
    db, _, _, event_id = fixture
    Worker(db, Recognizer(), DirectoryLedgerSink(tmp_path / "out")).run_once()
    with db.transaction() as connection:
        connection.execute(
            "UPDATE ingest_events SET last_error='sentinel-private-error'"
        )
        connection.execute(
            "UPDATE delivery_operations SET last_error='sentinel-private-error'"
        )
    client = TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path)))
    response = client.get(
        f"/v1/events/{event_id}", headers={"X-Ledger-Inbox-Key": "fixture-key"}
    )
    assert "sentinel-private-error" not in response.text
    assert str(tmp_path) not in response.text
    assert "external_ref" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_idempotency_conflicts_are_rejected_even_after_content_deduplication(fixture):
    db, tenant, service, _ = fixture
    service.ingest_image(
        tenant_id=tenant["id"],
        image=image_bytes(),
        filename="same.png",
        source_kind="https",
        source_event_id="new-key",
    )
    with pytest.raises(StateConflict):
        service.ingest_image(
            tenant_id=tenant["id"],
            image=image_bytes("blue"),
            filename="new.png",
            source_kind="https",
            source_event_id="new-key",
        )
    with db.session() as connection:
        assert (
            connection.execute("SELECT count(*) FROM ingest_events").fetchone()[0] == 1
        )


def test_backup_is_private_and_consistent(fixture, tmp_path):
    db, _, _, event_id = fixture
    destination = tmp_path / "backup.sqlite3"
    db.backup(destination)
    assert destination.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(destination) as backup:
        assert backup.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert backup.execute("SELECT id FROM ingest_events").fetchone()[0] == event_id
    with pytest.raises(ValueError):
        db.backup(destination)
