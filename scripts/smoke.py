"""Exercise real API/worker processes with synthetic images and an isolated local ledger.

With --url, run inside the disposable Compose api container sharing /data with worker.
No real model, mailbox, financial application or outbound notification is called.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path

import httpx
from PIL import Image

from ledger_inbox.database import Database
from ledger_inbox.security import sign_webhook


def run_cli(args, env):
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "from ledger_inbox.cli import main; import sys; sys.exit(main(sys.argv[1:]))",
            *args,
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def wait_event(client, event_id, status):
    for _ in range(100):
        event = client.get(f"/v1/events/{event_id}").json()
        if event["status"] == status:
            return event
        time.sleep(0.1)
    raise AssertionError(f"event did not reach {status}")


def exercise(url, env):
    if (
        env.get("LEDGER_INBOX_RECOGNIZER", "mock") != "mock"
        or env.get("LEDGER_INBOX_LEDGER_SINK", "directory") != "directory"
    ):
        raise ValueError("smoke requires disposable mock/directory services")
    checkpoint = Path(env["LEDGER_INBOX_DATA_DIR"]) / "smoke-checkpoint.json"
    if checkpoint.exists():
        previous = json.loads(checkpoint.read_text())
        response = httpx.get(
            url + "/v1/events/" + previous["event_id"],
            headers={"X-Ledger-Inbox-Key": previous["api_key"]},
            timeout=10,
            trust_env=False,
        )
        assert response.status_code == 200 and response.json()["status"] == "completed"
        assert response.json()["delivery"]["id"] == previous["operation_id"]
    tenant = run_cli(["create-tenant", "--name", "synthetic-smoke"], env)
    alias = run_cli(
        [
            "create-alias",
            "--tenant-id",
            tenant["id"],
            "--domain",
            "inbound.example.com",
        ],
        env,
    )["alias"]
    image = io.BytesIO()
    Image.new("RGB", (24, 24), "white").save(image, format="PNG")
    png = image.getvalue()
    with httpx.Client(
        base_url=url,
        trust_env=False,
        headers={"X-Ledger-Inbox-Key": tenant["api_key"]},
        timeout=10,
    ) as client:
        response = client.post(
            "/v1/ingest/images",
            files={"image": ("synthetic.png", png, "image/png")},
            headers={"Idempotency-Key": "fixture-first"},
        )
        assert response.status_code == 202, response.status_code
        event_id = response.json()["event_id"]
        event = wait_event(client, event_id, "needs_review")
        assert client.get(f"/v1/events/{event_id}/image").content == png
        message = EmailMessage()
        message["To"] = alias
        message.set_content("Synthetic test only")
        message.add_attachment(
            png, maintype="image", subtype="png", filename="synthetic.png"
        )
        raw = message.as_bytes()
        timestamp = str(int(time.time()))
        signed = client.post(
            "/v1/inbound/email/rfc822",
            content=raw,
            headers={
                "Content-Type": "message/rfc822",
                "X-Ledger-Inbox-Recipient": alias,
                "X-Ledger-Inbox-Timestamp": timestamp,
                "X-Ledger-Inbox-Signature": sign_webhook(
                    env["LEDGER_INBOX_EMAIL_WEBHOOK_SECRET"], timestamp, alias, raw
                ),
            },
        )
        assert signed.status_code == 202, signed.status_code
        assert signed.json()["events"][0]["event_id"] == event_id
        request = {
            "expected_version": event["version"],
            "action": "approve",
            "entry": {
                "transaction_type": "expense",
                "amount_minor": 1280,
                "currency": "CNY",
                "merchant": "Synthetic fixture",
                "occurred_at": "2026-10-09T12:00:00+08:00",
                "category": "food",
                "needs_review": False,
                "review_reasons": [],
            },
        }
        assert (
            client.post(f"/v1/events/{event_id}/review", json=request).status_code
            == 200
        )
        assert (
            client.post(f"/v1/events/{event_id}/review", json=request).status_code
            == 409
        )
        event = wait_event(client, event_id, "completed")
        assert "external_ref" not in event["delivery"]
        assert "image_path" not in event
        other = run_cli(["create-tenant", "--name", "other-smoke"], env)
        assert (
            client.get(
                f"/v1/events/{event_id}",
                headers={"X-Ledger-Inbox-Key": other["api_key"]},
            ).status_code
            == 404
        )
        assert client.post(f"/v1/events/{event_id}/reconcile").status_code == 200
        directory = Path(env["LEDGER_INBOX_DATA_DIR"]) / "ledger-outbox"
        operation = directory / f"{event['delivery']['id']}.json"
        assert json.loads(operation.read_text())["entry"]["amount_minor"] == 1280
        assert (
            sum(
                json.loads(file.read_text())["event_id"] == event_id
                for file in directory.glob("*.json")
            )
            == 1
        )
        descriptor = os.open(checkpoint, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(
                {
                    "event_id": event_id,
                    "api_key": tenant["api_key"],
                    "operation_id": event["delivery"]["id"],
                },
                output,
            )
        # Snapshot is private and readable; full disaster restore is described separately.
        with tempfile.TemporaryDirectory() as backup_root:
            run_cli(
                [
                    "backup",
                    "--destination",
                    str(Path(backup_root) / "snapshot.sqlite3"),
                ],
                env,
            )
            backup = Database(Path(backup_root) / "snapshot.sqlite3")
            assert backup.get_event(event_id, tenant["id"])["status"] == "completed"
    print(
        "smoke passed: HTTP + signed email + review + exactly one directory entry + tenant isolation + SQLite backup"
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url")
    args = parser.parse_args()
    if args.url:
        exercise(args.url, dict(os.environ))
        return
    with tempfile.TemporaryDirectory(prefix="ledger-inbox-smoke-") as root:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("LEDGER_INBOX_")
        }
        env.update(
            LEDGER_INBOX_DATA_DIR=root,
            LEDGER_INBOX_RECOGNIZER="mock",
            LEDGER_INBOX_LEDGER_SINK="directory",
            LEDGER_INBOX_EMAIL_WEBHOOK_SECRET="synthetic-smoke-secret",
        )
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        processes = []
        diagnostics = open(Path(root) / "process.log", "w+")
        try:
            for args in (
                ["serve", "--port", str(port)],
                ["worker", "--poll-seconds", "0.1"],
            ):
                processes.append(
                    subprocess.Popen(
                        [
                            sys.executable,
                            "-c",
                            "from ledger_inbox.cli import main; import sys; sys.exit(main(sys.argv[1:]))",
                            *args,
                        ],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=diagnostics,
                    )
                )
            url = f"http://127.0.0.1:{port}"
            for _ in range(100):
                try:
                    if (
                        httpx.get(
                            url + "/healthz", timeout=0.2, trust_env=False
                        ).status_code
                        == 200
                    ):
                        break
                except httpx.RequestError:
                    pass
                time.sleep(0.1)
            exercise(url, env)
        except BaseException:
            diagnostics.flush()
            diagnostics.seek(0)
            print(diagnostics.read(), file=sys.stderr)
            raise
        finally:
            for process in processes:
                process.terminate()
            for process in processes:
                process.wait(timeout=10)
            diagnostics.close()


if __name__ == "__main__":
    main()
