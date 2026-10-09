from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

from ledger_inbox.database import Database
from ledger_inbox.image_store import ImageStore
from ledger_inbox.ingestion import IngestionService
from ledger_inbox.security import new_api_key
from tests.helpers import image_bytes

PNG = image_bytes()


def make_service(tmp_path: Path) -> tuple[Database, IngestionService]:
    database = Database(tmp_path / "db.sqlite3")
    database.initialize()
    service = IngestionService(database, ImageStore(tmp_path / "images"), 1024 * 1024)
    return database, service


def test_deduplicates_same_image_within_tenant_but_not_across_tenants(
    tmp_path: Path,
) -> None:
    database, service = make_service(tmp_path)
    first_tenant = database.create_tenant("first", new_api_key())
    second_tenant = database.create_tenant("second", new_api_key())

    first = service.ingest_image(
        tenant_id=first_tenant["id"], image=PNG, filename="one.png", source_kind="https"
    )
    duplicate = service.ingest_image(
        tenant_id=first_tenant["id"], image=PNG, filename="two.png", source_kind="email"
    )
    other_tenant = service.ingest_image(
        tenant_id=second_tenant["id"],
        image=PNG,
        filename="one.png",
        source_kind="https",
    )

    assert duplicate.duplicate is True
    assert duplicate.event_id == first.event_id
    assert other_tenant.duplicate is False
    assert other_tenant.event_id != first.event_id


def test_email_extracts_only_image_attachments(tmp_path: Path) -> None:
    database, service = make_service(tmp_path)
    tenant = database.create_tenant("email user", new_api_key())
    message = EmailMessage()
    message["To"] = "alias@example.com"
    message["Message-ID"] = "<example-1>"
    message.set_content("This text is untrusted and must not become an instruction.")
    message.add_attachment(PNG, maintype="image", subtype="png", filename="receipt.png")
    message.add_attachment(
        b"ignore", maintype="text", subtype="plain", filename="prompt.txt"
    )

    results = service.ingest_email(
        tenant_id=tenant["id"],
        raw_message=message.as_bytes(),
        recipient="alias@example.com",
    )

    assert len(results) == 1
    event = database.get_event(results[0].event_id, tenant["id"])
    assert event is not None
    assert event["filename"] == "receipt.png"
    assert event["metadata"]["email_recipient"] == "alias@example.com"


def test_rejects_non_image_payload(tmp_path: Path) -> None:
    database, service = make_service(tmp_path)
    tenant = database.create_tenant("first", new_api_key())
    try:
        service.ingest_image(
            tenant_id=tenant["id"],
            image=b"not-an-image",
            filename="fake.png",
            source_kind="https",
        )
    except ValueError as exc:
        assert "supported" in str(exc)
    else:
        raise AssertionError("non-image payload was accepted")
