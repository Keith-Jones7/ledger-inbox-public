from __future__ import annotations

import time
from email.message import EmailMessage
from pathlib import Path

from fastapi.testclient import TestClient

from ledger_inbox.api import create_app
from ledger_inbox.config import Settings
from ledger_inbox.database import Database
from ledger_inbox.security import new_api_key, sign_webhook
from tests.helpers import image_bytes

PNG = image_bytes()


def make_client(tmp_path: Path) -> tuple[TestClient, Database, str, str]:
    settings = Settings(data_dir=tmp_path, webhook_secret="webhook-secret")
    database = Database(settings.db_path)
    database.initialize()
    api_key = new_api_key()
    tenant = database.create_tenant("test", api_key)
    alias = "li-test@example.com"
    database.add_alias(tenant["id"], alias)
    return TestClient(create_app(settings)), database, api_key, alias


def test_https_upload_and_tenant_scoped_status(tmp_path: Path) -> None:
    client, _, api_key, _ = make_client(tmp_path)
    response = client.post(
        "/v1/ingest/images",
        headers={"X-Ledger-Inbox-Key": api_key, "Idempotency-Key": "shortcut-run-1"},
        files={"image": ("receipt.png", PNG, "image/png")},
    )
    assert response.status_code == 202
    event_id = response.json()["event_id"]
    status_response = client.get(
        f"/v1/events/{event_id}", headers={"X-Ledger-Inbox-Key": api_key}
    )
    assert status_response.status_code == 200
    assert status_response.json()["status"] == "queued"
    assert "image_path" not in status_response.json()


def test_upload_requires_api_key(tmp_path: Path) -> None:
    client, _, _, _ = make_client(tmp_path)
    response = client.post(
        "/v1/ingest/images", files={"image": ("receipt.png", PNG, "image/png")}
    )
    assert response.status_code == 401


def test_signed_rfc822_webhook_routes_by_alias(tmp_path: Path) -> None:
    client, database, _, alias = make_client(tmp_path)
    message = EmailMessage()
    message["To"] = alias
    message["Message-ID"] = "<webhook-message>"
    message.set_content("receipt")
    message.add_attachment(PNG, maintype="image", subtype="png", filename="receipt.png")
    body = message.as_bytes()
    timestamp = str(int(time.time()))
    signature = sign_webhook("webhook-secret", timestamp, alias, body)

    response = client.post(
        "/v1/inbound/email/rfc822",
        content=body,
        headers={
            "Content-Type": "message/rfc822",
            "X-Ledger-Inbox-Recipient": alias,
            "X-Ledger-Inbox-Timestamp": timestamp,
            "X-Ledger-Inbox-Signature": signature,
        },
    )

    assert response.status_code == 202
    event_id = response.json()["events"][0]["event_id"]
    assert database.get_event(event_id)["source_kind"] == "email"


def test_rfc822_webhook_rejects_bad_signature(tmp_path: Path) -> None:
    client, _, _, alias = make_client(tmp_path)
    response = client.post(
        "/v1/inbound/email/rfc822",
        content=b"invalid",
        headers={
            "X-Ledger-Inbox-Recipient": alias,
            "X-Ledger-Inbox-Timestamp": str(int(time.time())),
            "X-Ledger-Inbox-Signature": "sha256=bad",
        },
    )
    assert response.status_code == 401
