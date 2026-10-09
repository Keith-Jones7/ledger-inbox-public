from __future__ import annotations

import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from ledger_inbox.adapters.ledgers import DirectoryLedgerSink
from ledger_inbox.api import create_app
from ledger_inbox.cli import main
from ledger_inbox.config import Settings
from ledger_inbox.database import SCHEMA, Database
from ledger_inbox.image_store import ImageStore
from ledger_inbox.ingestion import IngestionService
from ledger_inbox.worker import Worker
from tests.helpers import image_bytes
from tests.test_safety import ENTRY, Recognizer


def test_legacy_schema_migrates_without_replaying_writes(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as connection:
        # These are the original 0.1 table layouts, without leases/payload/audit/version columns.
        connection.executescript(SCHEMA)
        connection.execute(
            "INSERT INTO tenants VALUES('tenant','legacy','key',1,'2026-08-12')"
        )
        for event_id, status in [
            ("recognized", "recognized"),
            ("processing", "processing"),
            ("inflight", "delivering"),
            ("completed", "completed"),
        ]:
            connection.execute(
                """INSERT INTO ingest_events(id,tenant_id,source_kind,content_sha256,filename,media_type,image_path,metadata_json,status,created_at,updated_at)
                VALUES(?,'tenant','https',?,'fixture.png','image/png','unused','{}',?,'2026-08-12','2026-08-12')""",
                (event_id, event_id, status),
            )
            connection.execute(
                "INSERT INTO recognition_results VALUES(?,'fixture',?,'recognized','2026-08-12')",
                (event_id, json.dumps(ENTRY.to_dict())),
            )
        for event_id, state in [("inflight", "inflight"), ("completed", "confirmed")]:
            connection.execute(
                "INSERT INTO delivery_operations VALUES(?,?,'directory',?,NULL,NULL,'2026-08-12','2026-08-12')",
                (event_id, event_id, state),
            )
    db = Database(path)
    db.initialize()
    assert db.get_event("recognized")["status"] == "needs_review"
    assert db.get_event("inflight")["delivery"]["state"] == "inflight"
    assert db.get_event("inflight")["delivery"]["payload_json"] is not None
    assert db.get_event("completed")["status"] == "completed"
    db.initialize()
    assert db.claim_delivery("directory") is None
    with db.transaction() as connection:
        connection.execute(
            "UPDATE delivery_operations SET lease_until='2000-01-01T00:00:00+00:00' WHERE id='inflight'"
        )
    db.recover_expired()
    assert db.get_event("inflight")["delivery"]["state"] == "unknown"
    assert db.claim_delivery("directory") is None
    with db.session() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"


def test_future_schema_refuses_downgrade(tmp_path):
    path = tmp_path / "future.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version=999")
    with pytest.raises(ValueError):
        Database(path).initialize()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 999
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE name='tenants'"
            ).fetchone()[0]
            == 0
        )


def test_cli_default_workflow_approval_and_backup(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("LEDGER_INBOX_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("LEDGER_INBOX_RECOGNIZER", "mock")
    monkeypatch.setenv("LEDGER_INBOX_LEDGER_SINK", "directory")
    assert main(["init"]) == 0
    capsys.readouterr()
    assert main(["create-tenant", "--name", "fixture"]) == 0
    tenant = json.loads(capsys.readouterr().out)
    settings = Settings.from_env()
    db = Database(settings.db_path)
    service = IngestionService(
        db, ImageStore(settings.data_dir / "images"), settings.max_image_bytes
    )
    event_id = service.ingest_image(
        tenant_id=tenant["id"],
        image=image_bytes(),
        filename="fixture.png",
        source_kind="https",
    ).event_id
    assert main(["worker", "--once"]) == 0
    capsys.readouterr()
    assert main(["review", "export", event_id, "--tenant-id", tenant["id"]]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["event"]["status"] == "needs_review"
    assert exported["review_contract"]["policy"]["sha256"]
    entry = tmp_path / "entry.json"
    entry.write_text(json.dumps(ENTRY.to_dict()))
    assert (
        main(
            [
                "review",
                "approve",
                event_id,
                "--tenant-id",
                tenant["id"],
                "--expected-version",
                str(exported["event"]["version"]),
                "--entry-file",
                str(entry),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["worker", "--once"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "completed"
    assert main(["backup", "--destination", str(tmp_path / "snapshot.sqlite3")]) == 0
    assert json.loads(capsys.readouterr().out)["images_must_be_archived_separately"]


def test_manual_ignore_and_failed_recognition_retry(tmp_path):
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    tenant = db.create_tenant("fixture", "fixture-key")
    service = IngestionService(db, ImageStore(tmp_path / "images"), 1024 * 1024)
    event_id = service.ingest_image(
        tenant_id=tenant["id"],
        image=image_bytes(),
        filename="fixture.png",
        source_kind="https",
    ).event_id

    class FailedRecognizer(Recognizer):
        def recognize(self, context):
            raise ValueError("private-sentinel-error")

    Worker(db, FailedRecognizer(), DirectoryLedgerSink(tmp_path / "out")).run_once()
    client = TestClient(create_app(Settings(data_dir=tmp_path, database_path=db.path)))
    headers = {"X-Ledger-Inbox-Key": "fixture-key"}
    failed = client.get(f"/v1/events/{event_id}", headers=headers).json()
    assert "private-sentinel-error" not in json.dumps(failed)
    assert (
        client.post(
            f"/v1/events/{event_id}/review",
            headers=headers,
            json={"expected_version": True, "action": "retry_recognition"},
        ).status_code
        == 422
    )
    retried = client.post(
        f"/v1/events/{event_id}/review",
        headers=headers,
        json={"expected_version": failed["version"], "action": "retry_recognition"},
    )
    assert retried.json()["status"] == "queued"
    Worker(db, Recognizer(ENTRY), DirectoryLedgerSink(tmp_path / "out")).run_once()
    assert (
        client.post(
            f"/v1/events/{event_id}/review",
            headers=headers,
            json={"expected_version": failed["version"], "action": "ignore"},
        ).status_code
        == 409
    )
