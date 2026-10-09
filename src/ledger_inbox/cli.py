from __future__ import annotations

import argparse
import json
import os
from collections.abc import Sequence
from pathlib import Path

import uvicorn

from .adapters.ledgers import load_ledger_sink
from .adapters.recognizers import load_recognizer
from .api import create_app
from .config import Settings
from .database import Database, StateConflict, public_event
from .email_connector import IMAPConnector, IMAPSettings
from .image_store import ImageStore
from .ingestion import IngestionService
from .review import apply_review, review_contract
from .security import new_api_key, new_inbound_alias
from .worker import Worker


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ledger-inbox")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init", help="initialize/migrate the private SQLite store")
    tenant = subparsers.add_parser("create-tenant")
    tenant.add_argument("--name", required=True)
    alias = subparsers.add_parser("create-alias")
    alias.add_argument("--tenant-id", required=True)
    alias.add_argument("--alias")
    alias.add_argument("--domain", default="inbound.invalid")
    serve = subparsers.add_parser("serve")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    for command, interval in [("worker", 2), ("imap", 30)]:
        runner = subparsers.add_parser(command)
        runner.add_argument("--once", action="store_true")
        runner.add_argument("--poll-seconds", type=float, default=interval)
    review = subparsers.add_parser(
        "review", help="inspect, correct and resolve tenant events"
    )
    actions = review.add_subparsers(dest="action", required=True)
    for action in ("list", "export", "approve", "ignore", "retry", "reconcile"):
        item = actions.add_parser(action)
        item.add_argument("--tenant-id", required=True)
        if action != "list":
            item.add_argument("event_id")
        if action in {"approve", "ignore", "retry"}:
            item.add_argument("--expected-version", type=int, required=True)
        if action == "list":
            item.add_argument("--status", default="needs_review")
        if action == "approve":
            item.add_argument("--entry-file", type=Path, required=True)
    backup = subparsers.add_parser(
        "backup", help="create a consistent SQLite backup; archive images separately"
    )
    backup.add_argument("--destination", type=Path, required=True)
    return parser


def _print(payload: dict | list | None) -> None:
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "poll_seconds", 1) <= 0:
        parser.error("poll interval must be positive")
    try:
        settings = Settings.from_env()
        database = Database(settings.db_path)
        database.initialize()
        if args.command == "init":
            _print({"database": str(settings.db_path), "schema_version": 2})
        elif args.command == "create-tenant":
            api_key = new_api_key()
            _print({**database.create_tenant(args.name, api_key), "api_key": api_key})
        elif args.command == "create-alias":
            alias = args.alias or new_inbound_alias(args.domain)
            database.add_alias(args.tenant_id, alias)
            _print({"tenant_id": args.tenant_id, "alias": alias})
        elif args.command == "serve":
            uvicorn.run(
                create_app(settings),
                host=args.host,
                port=args.port,
                proxy_headers=settings.trust_proxy_headers,
            )
        elif args.command == "worker":
            worker = Worker(
                database,
                load_recognizer(settings.recognizer, settings),
                load_ledger_sink(settings.ledger_sink, settings),
                settings,
            )
            if args.once:
                _print(worker.run_once())
            else:
                worker.run_forever(args.poll_seconds)
        elif args.command == "imap":
            connector = _imap_connector(settings, database)
            if args.once:
                _print(connector.poll_once())
            else:
                connector.run_forever(args.poll_seconds)
        elif args.command == "backup":
            database.backup(args.destination)
            _print(
                {
                    "database_backup": str(args.destination),
                    "images_must_be_archived_separately": True,
                }
            )
        elif args.command == "review":
            if args.action == "list":
                _print({"events": database.list_events(args.tenant_id, args.status)})
                return 0
            event = database.get_event(args.event_id, args.tenant_id)
            if event is None:
                raise KeyError("event not found")
            if args.action == "export":
                # Local operator export deliberately includes the original's private path.
                _print(
                    {
                        "event": public_event(event),
                        "image_path": event["image_path"],
                        "review_contract": review_contract(settings),
                    }
                )
            elif args.action == "reconcile":
                worker = Worker(
                    database,
                    load_recognizer("mock", settings),
                    load_ledger_sink(settings.ledger_sink, settings),
                    settings,
                )
                _print(worker.reconcile(args.event_id, args.tenant_id))
            else:
                payload = (
                    json.loads(args.entry_file.read_text())
                    if args.action == "approve"
                    else None
                )
                apply_review(
                    database,
                    settings,
                    args.event_id,
                    args.tenant_id,
                    args.expected_version,
                    "retry_recognition" if args.action == "retry" else args.action,
                    payload,
                )
                _print(public_event(database.get_event(args.event_id, args.tenant_id)))
    except (ValueError, StateConflict, KeyError):
        # Do not echo adapter config, image text or credentials on failure.
        parser.exit(
            2,
            "operation rejected; verify configuration, entry fields and current event version\n",
        )
    return 0


def _imap_connector(settings: Settings, database: Database) -> IMAPConnector:
    required = {
        name: os.getenv(f"LEDGER_INBOX_IMAP_{name.upper()}")
        for name in ("host", "username", "password")
    }
    if not all(required.values()):
        raise ValueError("IMAP host, username and password are required")
    return IMAPConnector(
        IMAPSettings(
            **required,
            mailbox=os.getenv("LEDGER_INBOX_IMAP_MAILBOX", "INBOX"),
            port=int(os.getenv("LEDGER_INBOX_IMAP_PORT", "993")),
        ),
        database,
        IngestionService(
            database,
            ImageStore(settings.data_dir / "images", settings.max_image_pixels),
            settings.max_image_bytes,
            settings.max_email_bytes,
        ),
    )
