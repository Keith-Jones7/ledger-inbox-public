from __future__ import annotations

import json
from email.message import EmailMessage

import httpx
import pytest
from fastapi.testclient import TestClient

from ledger_inbox.adapters.ledgers import (
    UnknownDeliveryError,
    WebhookLedgerSink,
    operation_payload,
    payload_hash,
)
from ledger_inbox.adapters.recognizers import OpenAICompatibleRecognizer
from ledger_inbox.api import create_app
from ledger_inbox.config import Settings
from ledger_inbox.database import Database
from ledger_inbox.domain import RecognitionContext
from ledger_inbox.email_connector import IMAPConnector, IMAPSettings
from ledger_inbox.image_store import ImageStore, detect_image_type
from ledger_inbox.ingestion import IngestionService
from tests.helpers import image_bytes
from tests.test_safety import ENTRY


@pytest.mark.parametrize("status", [202, 204, 301, 400, 409, 429, 500, 503])
def test_nonconfirming_http_responses_remain_unknown(monkeypatch, status):
    monkeypatch.setattr(httpx, "post", lambda *a, **kw: httpx.Response(status))
    with pytest.raises(UnknownDeliveryError):
        WebhookLedgerSink("https://fixture.example/ledger").deliver(
            ENTRY, event_id="event", operation_id="op"
        )


@pytest.mark.parametrize(
    "changed",
    [
        {"operation_id": "other"},
        {"payload_sha256": "wrong"},
        {"state": "accepted"},
        {"external_ref": None},
    ],
)
def test_ledger_confirmation_binds_exact_operation_and_payload(monkeypatch, changed):
    payload = operation_payload(ENTRY, "event", "op")
    receipt = {
        "state": "confirmed",
        "operation_id": "op",
        "payload_sha256": payload_hash(payload),
        "external_ref": "ledger-row",
    }
    monkeypatch.setattr(
        httpx, "post", lambda *a, **kw: httpx.Response(200, json={**receipt, **changed})
    )
    with pytest.raises(UnknownDeliveryError):
        WebhookLedgerSink("https://fixture.example/ledger").deliver(
            ENTRY, event_id="event", operation_id="op"
        )


def test_ledger_delivery_and_read_only_reconciliation(monkeypatch):
    calls = []
    payload = operation_payload(ENTRY, "event", "op")

    def post(url, **kwargs):
        calls.append((url, json.loads(kwargs["content"])))
        return httpx.Response(
            200,
            json={
                "state": "confirmed",
                "operation_id": "op",
                "payload_sha256": payload_hash(payload),
                "external_ref": "ledger-row",
            },
        )

    monkeypatch.setattr(httpx, "post", post)
    sink = WebhookLedgerSink(
        "https://fixture.example/write",
        "fixture-secret",
        "https://fixture.example/query",
    )
    assert sink.deliver(ENTRY, event_id="event", operation_id="op") == "ledger-row"
    assert sink.reconcile(ENTRY, event_id="event", operation_id="op") == "ledger-row"
    assert calls[1] == ("https://fixture.example/query", {"operation_id": "op"})


def mail(alias, image=None):
    message = EmailMessage()
    message["To"] = alias
    message.set_content("Fixture only")
    if image:
        message.add_attachment(
            image, maintype="image", subtype="png", filename="fixture.png"
        )
    return message.as_bytes()


class FakeIMAP:
    validity = b"10"
    messages = {}
    seen = []
    fetched = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def login(self, *args):
        return "OK", []

    def select(self, *args):
        return "OK", []

    def response(self, *args):
        return "UIDVALIDITY", [self.validity]

    def uid(self, command, *args):
        if command == "search":
            return "OK", [b" ".join(self.messages)]
        if command == "fetch":
            uid, kind = args
            if kind == "(RFC822.SIZE)":
                return "OK", [f"1 (RFC822.SIZE {len(self.messages[uid])})".encode()]
            self.fetched.append(uid)
            return "OK", [(b"BODY", self.messages[uid])]
        self.seen.append(args[0])
        return "OK", []


def connector(tmp_path, monkeypatch, messages):
    FakeIMAP.messages, FakeIMAP.seen, FakeIMAP.fetched = messages, [], []
    FakeIMAP.validity = b"10"
    monkeypatch.setattr("ledger_inbox.email_connector.imaplib.IMAP4_SSL", FakeIMAP)
    db = Database(tmp_path / "db.sqlite3")
    db.initialize()
    tenant = db.create_tenant("fixture", "fixture-key")
    db.add_alias(tenant["id"], "fixture@example.com")
    service = IngestionService(db, ImageStore(tmp_path / "images"), 1024 * 1024)
    return IMAPConnector(
        IMAPSettings("imap.example.com", "fixture", "fixture"), db, service
    ), db


def test_no_image_mail_does_not_block_later_mail(tmp_path, monkeypatch):
    runner, db = connector(
        tmp_path,
        monkeypatch,
        {
            b"1": mail("fixture@example.com"),
            b"2": mail("fixture@example.com", image_bytes()),
        },
    )
    assert runner.poll_once() == {"accepted_messages": 1, "skipped_messages": 1}
    assert FakeIMAP.seen == [b"2"]
    with db.session() as connection:
        assert (
            connection.execute("SELECT count(*) FROM ingest_events").fetchone()[0] == 1
        )
        assert (
            connection.execute("SELECT error_code FROM ingress_errors").fetchone()[0]
            == "imap_message_skipped"
        )


def test_uidvalidity_change_creates_new_event_for_reused_uid(tmp_path, monkeypatch):
    runner, db = connector(
        tmp_path, monkeypatch, {b"1": mail("fixture@example.com", image_bytes())}
    )
    runner.poll_once()
    FakeIMAP.messages = {b"1": mail("fixture@example.com", image_bytes("blue"))}
    FakeIMAP.validity = b"11"
    runner.poll_once()
    with db.session() as connection:
        assert (
            connection.execute("SELECT count(*) FROM ingest_events").fetchone()[0] == 2
        )


def test_oversized_mail_is_not_fetched_or_marked_seen(tmp_path, monkeypatch):
    runner, _ = connector(
        tmp_path, monkeypatch, {b"1": mail("fixture@example.com", image_bytes())}
    )
    runner.ingestion.max_email_bytes = 10
    assert runner.poll_once()["skipped_messages"] == 1
    assert FakeIMAP.fetched == [] and FakeIMAP.seen == []


def test_failed_seen_update_is_safe_to_ingest_again(tmp_path, monkeypatch):
    runner, db = connector(
        tmp_path, monkeypatch, {b"1": mail("fixture@example.com", image_bytes())}
    )
    original = FakeIMAP.uid

    def uid(self, command, *args):
        return ("NO", []) if command == "store" else original(self, command, *args)

    monkeypatch.setattr(FakeIMAP, "uid", uid)
    assert runner.poll_once()["skipped_messages"] == 1
    assert runner.poll_once()["skipped_messages"] == 1
    with db.session() as connection:
        assert (
            connection.execute("SELECT count(*) FROM ingest_events").fetchone()[0] == 1
        )


def test_imap_account_identity_is_part_of_source_id(tmp_path, monkeypatch):
    runner, db = connector(
        tmp_path, monkeypatch, {b"1": mail("fixture@example.com", image_bytes())}
    )
    runner.poll_once()
    runner.config = IMAPSettings("imap.example.com", "other-fixture", "fixture")
    FakeIMAP.messages = {b"1": mail("fixture@example.com", image_bytes("blue"))}
    runner.poll_once()
    with db.session() as connection:
        assert (
            connection.execute("SELECT count(*) FROM ingest_events").fetchone()[0] == 2
        )


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP", "HEIF", "AVIF"])
def test_real_images_decode(format):
    assert detect_image_type(image_bytes(format=format))[1].startswith("image/")


def test_fake_png_header_is_rejected():
    with pytest.raises(ValueError):
        detect_image_type(b"\x89PNG\r\n\x1a\ninvalid")


def test_pixel_limit_is_enforced():
    with pytest.raises(ValueError):
        detect_image_type(image_bytes(), max_pixels=100)


def test_streamed_mail_limit_applies_without_content_length(tmp_path):
    client = TestClient(
        create_app(
            Settings(data_dir=tmp_path, webhook_secret="fixture", max_email_bytes=10)
        )
    )
    response = client.post(
        "/v1/inbound/email/rfc822",
        content=iter([b"123456", b"789012"]),
        headers={
            "X-Ledger-Inbox-Recipient": "fixture@example.com",
            "X-Ledger-Inbox-Timestamp": "1",
            "X-Ledger-Inbox-Signature": "fixture",
        },
    )
    assert response.status_code == 413


def test_multipart_limit_applies_before_spooling(tmp_path):
    client = TestClient(create_app(Settings(data_dir=tmp_path, max_image_bytes=10)))
    response = client.post(
        "/v1/ingest/images",
        content=iter([b"x" * 40000, b"x" * 40000]),
        headers={"Content-Type": "multipart/form-data; boundary=fixture"},
    )
    assert response.status_code == 413


@pytest.mark.parametrize(
    "shadow",
    [
        {**ENTRY.to_dict(), "amount_minor": 999},
        {**ENTRY.to_dict(), "needs_review": True},
        None,
    ],
)
def test_independent_shadow_disagreement_or_failure_blocks_writing(
    tmp_path, monkeypatch, shadow
):
    path = tmp_path / "fixture.png"
    path.write_bytes(image_bytes())
    settings = Settings(data_dir=tmp_path, shadow_model="shadow-model")
    requests = []

    def post(url, **kwargs):
        requests.append(kwargs["json"])
        if len(requests) == 2 and shadow is None:
            raise httpx.ConnectError("fixture failure")
        result = ENTRY.to_dict() if len(requests) == 1 else shadow
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json.dumps(result)}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(httpx, "post", post)
    recognizer = OpenAICompatibleRecognizer(settings)
    result = recognizer.recognize(
        RecognitionContext(
            "fixture", "tenant", str(path), "image/png", "fixture.png", None, {}
        )
    )
    assert result.needs_review
    assert (
        requests[0]["messages"][1]["content"] == requests[1]["messages"][1]["content"]
    )
    assert requests[1]["model"] == "shadow-model"
    assert "primary_raw" in recognizer.last_audit
    assert requests[0]["response_format"]["type"] == "json_schema"
    assert requests[0]["reasoning_effort"] == "none"


def test_remote_cleartext_model_endpoint_is_rejected():
    with pytest.raises(ValueError):
        Settings(model_base_url="http://remote.example/v1")
