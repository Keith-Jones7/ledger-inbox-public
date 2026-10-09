from __future__ import annotations

import json
import os
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .security import hash_api_key


class StateConflict(ValueError):
    """An expired claim, conflicting idempotency key or stale review was rejected."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _deadline(seconds: int) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS tenants (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, api_key_hash TEXT NOT NULL UNIQUE,
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS inbound_aliases (
 alias TEXT PRIMARY KEY COLLATE NOCASE, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingest_events (
 id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL REFERENCES tenants(id),
 source_kind TEXT NOT NULL, source_event_id TEXT, content_sha256 TEXT NOT NULL,
 filename TEXT NOT NULL, media_type TEXT NOT NULL, image_path TEXT NOT NULL,
 captured_at TEXT, metadata_json TEXT NOT NULL, status TEXT NOT NULL,
 attempt_count INTEGER NOT NULL DEFAULT 0, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
 UNIQUE(tenant_id,content_sha256), UNIQUE(tenant_id,source_kind,source_event_id)
);
CREATE INDEX IF NOT EXISTS idx_ingest_events_status ON ingest_events(status,created_at);
CREATE TABLE IF NOT EXISTS ingest_keys (
 tenant_id TEXT NOT NULL REFERENCES tenants(id), source_kind TEXT NOT NULL,
 source_id TEXT NOT NULL, content_sha256 TEXT NOT NULL, event_id TEXT NOT NULL REFERENCES ingest_events(id),
 PRIMARY KEY(tenant_id,source_kind,source_id)
);
CREATE TABLE IF NOT EXISTS recognition_results (
 event_id TEXT PRIMARY KEY REFERENCES ingest_events(id), recognizer TEXT NOT NULL,
 entry_json TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS delivery_operations (
 id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE REFERENCES ingest_events(id),
 sink TEXT NOT NULL, state TEXT NOT NULL, external_ref TEXT, last_error TEXT,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS review_history (
 id INTEGER PRIMARY KEY, event_id TEXT NOT NULL REFERENCES ingest_events(id),
 action TEXT NOT NULL, actor TEXT NOT NULL, before_json TEXT NOT NULL,
 after_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ingress_errors (
 id INTEGER PRIMARY KEY, source_kind TEXT NOT NULL, source_id TEXT NOT NULL,
 error_code TEXT NOT NULL, created_at TEXT NOT NULL, UNIQUE(source_kind,source_id)
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        os.chmod(self.path, 0o600)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        deadline = time.monotonic() + 10
        while True:
            try:
                connection.execute("PRAGMA journal_mode=WAL")
                return connection
            except sqlite3.OperationalError as exc:
                if (
                    getattr(exc, "sqlite_errorcode", 0) & 255
                    not in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
                    or time.monotonic() >= deadline
                ):
                    connection.close()
                    raise
                time.sleep(0.05)

    @contextmanager
    def transaction(self, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
        with self.session() as connection:
            try:
                connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
                yield connection
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.transaction(immediate=True) as connection:
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version > 2:
                raise ValueError("database schema is newer than this application")
            for statement in SCHEMA.split(";"):
                if statement.strip():
                    connection.execute(statement)
            additions = {
                "ingest_events": {
                    "version": "INTEGER NOT NULL DEFAULT 1",
                    "lease_token": "TEXT",
                    "lease_until": "TEXT",
                },
                "recognition_results": {"audit_json": "TEXT NOT NULL DEFAULT '{}'"},
                "delivery_operations": {
                    "payload_json": "TEXT",
                    "lease_until": "TEXT",
                    "dispatch_token": "TEXT",
                },
            }
            for table, columns in additions.items():
                existing = {
                    row["name"]
                    for row in connection.execute(f"PRAGMA table_info({table})")
                }
                for column, definition in columns.items():
                    if column not in existing:
                        connection.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            connection.execute("""INSERT OR IGNORE INTO ingest_keys(tenant_id,source_kind,source_id,content_sha256,event_id)
                SELECT tenant_id,source_kind,source_event_id,content_sha256,id FROM ingest_events WHERE source_event_id IS NOT NULL""")
            if version < 2:
                # Give existing workers a grace period; ambiguous writes are never requeued.
                connection.execute(
                    "UPDATE ingest_events SET lease_until=? WHERE status='processing' AND lease_until IS NULL",
                    (_deadline(600),),
                )
                connection.execute(
                    "UPDATE delivery_operations SET lease_until=? WHERE state='inflight' AND lease_until IS NULL",
                    (_deadline(600),),
                )
                connection.execute("""UPDATE delivery_operations SET payload_json=(
                    SELECT entry_json FROM recognition_results WHERE event_id=delivery_operations.event_id)
                    WHERE payload_json IS NULL""")
            if version < 2:
                connection.execute(
                    "UPDATE ingest_events SET status='needs_review',version=version+1,last_error='upgrade_requires_review' WHERE status='recognized' AND NOT EXISTS(SELECT 1 FROM delivery_operations d WHERE d.event_id=ingest_events.id)"
                )
            if version < 2:
                rows = connection.execute(
                    "SELECT r.event_id,r.entry_json FROM recognition_results r JOIN ingest_events e ON e.id=r.event_id WHERE e.last_error='upgrade_requires_review'"
                ).fetchall()
                for row in rows:
                    payload = json.loads(row["entry_json"])
                    payload["needs_review"] = True
                    payload["review_reasons"] = list(
                        dict.fromkeys(
                            [
                                *payload.get("review_reasons", []),
                                "upgrade_requires_review",
                            ]
                        )
                    )
                    connection.execute(
                        "UPDATE recognition_results SET status='needs_review',entry_json=? WHERE event_id=?",
                        (_json(payload), row["event_id"]),
                    )
            connection.execute("PRAGMA user_version=2")

    def create_tenant(self, name: str, api_key: str) -> dict[str, str]:
        tenant_id = str(uuid.uuid4())
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO tenants(id,name,api_key_hash,created_at) VALUES(?,?,?,?)",
                (tenant_id, name, hash_api_key(api_key), utc_now()),
            )
        return {"id": tenant_id, "name": name}

    def add_alias(self, tenant_id: str, alias: str) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO inbound_aliases(alias,tenant_id,created_at) VALUES(?,?,?)",
                (alias.lower(), tenant_id, utc_now()),
            )

    def tenant_for_api_key(self, api_key: str) -> dict | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT id,name FROM tenants WHERE api_key_hash=? AND active=1",
                (hash_api_key(api_key),),
            ).fetchone()
        return dict(row) if row else None

    def tenant_for_alias(self, alias: str) -> dict | None:
        with self.session() as connection:
            row = connection.execute(
                """SELECT t.id,t.name FROM tenants t JOIN inbound_aliases a ON a.tenant_id=t.id
                WHERE a.alias=? AND a.active=1 AND t.active=1""",
                (alias.lower(),),
            ).fetchone()
        return dict(row) if row else None

    def create_event(
        self,
        *,
        tenant_id: str,
        source_kind: str,
        source_event_id: str | None,
        digest: str,
        filename: str,
        media_type: str,
        image_path: str,
        captured_at: str | None,
        metadata: dict,
    ) -> tuple[dict, bool]:
        now = utc_now()
        event_id = str(uuid.uuid4())
        with self.transaction(immediate=True) as connection:
            if source_event_id:
                previous = connection.execute(
                    "SELECT e.*,k.content_sha256 AS key_digest FROM ingest_keys k JOIN ingest_events e ON e.id=k.event_id WHERE k.tenant_id=? AND k.source_kind=? AND k.source_id=?",
                    (tenant_id, source_kind, source_event_id),
                ).fetchone()
                if previous:
                    if previous["key_digest"] != digest:
                        raise StateConflict(
                            "idempotency key already belongs to different image content"
                        )
                    return _event_dict(previous), True
            previous = connection.execute(
                "SELECT * FROM ingest_events WHERE tenant_id=? AND content_sha256=?",
                (tenant_id, digest),
            ).fetchone()
            if previous:
                if source_event_id:
                    connection.execute(
                        "INSERT INTO ingest_keys VALUES(?,?,?,?,?)",
                        (
                            tenant_id,
                            source_kind,
                            source_event_id,
                            digest,
                            previous["id"],
                        ),
                    )
                return _event_dict(previous), True
            connection.execute(
                """INSERT INTO ingest_events(id,tenant_id,source_kind,source_event_id,content_sha256,
                filename,media_type,image_path,captured_at,metadata_json,status,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)""",
                (
                    event_id,
                    tenant_id,
                    source_kind,
                    source_event_id,
                    digest,
                    filename,
                    media_type,
                    image_path,
                    captured_at,
                    _json(metadata),
                    now,
                    now,
                ),
            )
            if source_event_id:
                connection.execute(
                    "INSERT INTO ingest_keys VALUES(?,?,?,?,?)",
                    (tenant_id, source_kind, source_event_id, digest, event_id),
                )
            return _event_dict(
                connection.execute(
                    "SELECT * FROM ingest_events WHERE id=?", (event_id,)
                ).fetchone()
            ), False

    def get_event(self, event_id: str, tenant_id: str | None = None) -> dict | None:
        with self.session() as connection:
            query = "SELECT * FROM ingest_events WHERE id=?"
            params = (event_id,)
            if tenant_id is not None:
                query += " AND tenant_id=?"
                params += (tenant_id,)
            row = connection.execute(query, params).fetchone()
            if row is None:
                return None
            event = _event_dict(row)
            recognition = connection.execute(
                "SELECT * FROM recognition_results WHERE event_id=?", (event_id,)
            ).fetchone()
            delivery = connection.execute(
                "SELECT * FROM delivery_operations WHERE event_id=?", (event_id,)
            ).fetchone()
            history = connection.execute(
                "SELECT action,actor,before_json,after_json,created_at FROM review_history WHERE event_id=? ORDER BY id",
                (event_id,),
            ).fetchall()
        if recognition:
            event["recognition"] = {
                "recognizer": recognition["recognizer"],
                "entry": json.loads(recognition["entry_json"]),
                "status": recognition["status"],
                "audit": json.loads(recognition["audit_json"]),
                "created_at": recognition["created_at"],
            }
        if delivery:
            event["delivery"] = dict(delivery)
        event["reviews"] = [
            {
                "action": r["action"],
                "actor": r["actor"],
                "before": json.loads(r["before_json"]),
                "after": json.loads(r["after_json"]),
                "created_at": r["created_at"],
            }
            for r in history
        ]
        return event

    def list_events(
        self, tenant_id: str, status: str = "needs_review", limit: int = 100
    ) -> list[dict]:
        with self.session() as connection:
            rows = connection.execute(
                "SELECT id,status,version,created_at FROM ingest_events WHERE tenant_id=? AND status=? ORDER BY created_at LIMIT ?",
                (tenant_id, status, min(max(limit, 1), 100)),
            ).fetchall()
        return [dict(row) for row in rows]

    def recover_expired(self, max_attempts: int = 3) -> None:
        now = utc_now()
        with self.transaction(immediate=True) as connection:
            connection.execute(
                """UPDATE ingest_events SET status=CASE WHEN attempt_count>=? THEN 'recognition_failed' ELSE 'queued' END,
                last_error='recognition_lease_expired', lease_token=NULL,lease_until=NULL,version=version+1,updated_at=?
                WHERE status='processing' AND lease_until<=?""",
                (max_attempts, now, now),
            )
            operations = connection.execute(
                "SELECT id,event_id FROM delivery_operations WHERE state='inflight' AND lease_until<=?",
                (now,),
            ).fetchall()
            for operation in operations:
                connection.execute(
                    "UPDATE delivery_operations SET state='unknown',last_error='dispatch_lease_expired',updated_at=? WHERE id=? AND state='inflight'",
                    (now, operation["id"]),
                )
                connection.execute(
                    "UPDATE ingest_events SET status='delivery_unknown',last_error='dispatch_lease_expired',version=version+1,updated_at=? WHERE id=?",
                    (now, operation["event_id"]),
                )

    def claim_next_event(self, lease_seconds: int = 600) -> dict | None:
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT e.* FROM ingest_events e JOIN tenants t ON t.id=e.tenant_id WHERE e.status='queued' AND t.active=1 ORDER BY e.created_at LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            token = str(uuid.uuid4())
            connection.execute(
                "UPDATE ingest_events SET status='processing',attempt_count=attempt_count+1,lease_token=?,lease_until=?,version=version+1,updated_at=? WHERE id=? AND status='queued'",
                (token, _deadline(lease_seconds), utc_now(), row["id"]),
            )
            return _event_dict(
                connection.execute(
                    "SELECT * FROM ingest_events WHERE id=?", (row["id"],)
                ).fetchone()
            )

    def fail_recognition(self, event_id: str, error: str, *, lease_token: str) -> None:
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE ingest_events SET status='recognition_failed',last_error=?,lease_token=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=? AND status='processing' AND lease_token=? AND lease_until>?",
                (error, utc_now(), event_id, lease_token, utc_now()),
            )
            if updated.rowcount != 1:
                raise StateConflict("recognition claim expired")

    def save_recognition(
        self,
        event_id: str,
        recognizer: str,
        entry: dict,
        needs_review: bool,
        *,
        lease_token: str,
        audit: dict | None = None,
    ) -> None:
        status = "needs_review" if needs_review else "recognized"
        with self.transaction(immediate=True) as connection:
            updated = connection.execute(
                "UPDATE ingest_events SET status=?,last_error=NULL,lease_token=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=? AND status='processing' AND lease_token=? AND lease_until>?",
                (status, utc_now(), event_id, lease_token, utc_now()),
            )
            if updated.rowcount != 1:
                raise StateConflict("recognition claim expired")
            connection.execute(
                """INSERT INTO recognition_results(event_id,recognizer,entry_json,status,created_at,audit_json)
                VALUES(?,?,?,?,?,?) ON CONFLICT(event_id) DO UPDATE SET recognizer=excluded.recognizer,
                entry_json=excluded.entry_json,status=excluded.status,created_at=excluded.created_at,audit_json=excluded.audit_json""",
                (
                    event_id,
                    recognizer,
                    _json(entry),
                    status,
                    utc_now(),
                    _json(audit or {}),
                ),
            )

    def prepare_delivery(self, event_id: str, sink: str) -> str:
        with self.transaction(immediate=True) as connection:
            previous = connection.execute(
                "SELECT id,sink FROM delivery_operations WHERE event_id=?", (event_id,)
            ).fetchone()
            if previous:
                if previous["sink"] != sink:
                    raise StateConflict("operation is pinned to another sink")
                return previous["id"]
            row = connection.execute(
                "SELECT r.entry_json FROM ingest_events e JOIN recognition_results r ON r.event_id=e.id WHERE e.id=? AND e.status='recognized'",
                (event_id,),
            ).fetchone()
            if row is None:
                raise StateConflict("event is not ready for delivery")
            operation_id = str(
                uuid.uuid5(uuid.NAMESPACE_URL, "ledger-inbox:" + event_id)
            )
            now = utc_now()
            connection.execute(
                "INSERT INTO delivery_operations(id,event_id,sink,state,payload_json,created_at,updated_at) VALUES(?,?,?,'pending',?,?,?)",
                (operation_id, event_id, sink, row["entry_json"], now, now),
            )
            return operation_id

    def next_recognized(self) -> str | None:
        with self.session() as connection:
            row = connection.execute(
                "SELECT e.id FROM ingest_events e JOIN tenants t ON t.id=e.tenant_id WHERE e.status='recognized' AND t.active=1 AND NOT EXISTS(SELECT 1 FROM delivery_operations d WHERE d.event_id=e.id) ORDER BY e.created_at LIMIT 1"
            ).fetchone()
        return row["id"] if row else None

    def claim_delivery(self, sink: str, lease_seconds: int = 600) -> dict | None:
        with self.transaction(immediate=True) as connection:
            operation = connection.execute(
                "SELECT d.* FROM delivery_operations d JOIN ingest_events e ON e.id=d.event_id JOIN tenants t ON t.id=e.tenant_id WHERE d.state='pending' AND d.sink=? AND e.status='recognized' AND t.active=1 ORDER BY d.created_at LIMIT 1",
                (sink,),
            ).fetchone()
            if operation is None:
                return None
            now = utc_now()
            token = str(uuid.uuid4())
            connection.execute(
                "UPDATE delivery_operations SET state='inflight',lease_until=?,dispatch_token=?,updated_at=? WHERE id=? AND state='pending'",
                (_deadline(lease_seconds), token, now, operation["id"]),
            )
            connection.execute(
                "UPDATE ingest_events SET status='delivering',version=version+1,updated_at=? WHERE id=? AND status='recognized'",
                (now, operation["event_id"]),
            )
            return dict(
                connection.execute(
                    "SELECT * FROM delivery_operations WHERE id=?", (operation["id"],)
                ).fetchone()
            )

    def finish_delivery(
        self,
        event_id: str,
        operation_id: str,
        *,
        state: str,
        external_ref: str | None = None,
        error: str | None = None,
        dispatch_token: str,
    ) -> str:
        if state not in {"confirmed", "failed", "unknown"}:
            raise ValueError("invalid delivery result")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM delivery_operations WHERE id=? AND event_id=?",
                (operation_id, event_id),
            ).fetchone()
            if row is None or row["dispatch_token"] != dispatch_token:
                raise StateConflict("delivery claim does not match")
            # A delayed confirmed result can resolve lease expiry, but stale failures cannot downgrade it.
            if row["state"] == "inflight" or (
                row["state"] == "unknown" and state == "confirmed"
            ):
                now = utc_now()
                connection.execute(
                    "UPDATE delivery_operations SET state=?,external_ref=?,last_error=?,updated_at=? WHERE id=?",
                    (state, external_ref, error, now, operation_id),
                )
                status = {
                    "confirmed": "completed",
                    "failed": "delivery_failed",
                    "unknown": "delivery_unknown",
                }[state]
                connection.execute(
                    "UPDATE ingest_events SET status=?,last_error=?,version=version+1,updated_at=? WHERE id=?",
                    (status, error, now, event_id),
                )
            current = connection.execute(
                "SELECT status FROM ingest_events WHERE id=?", (event_id,)
            ).fetchone()
            return current["status"]

    def review_event(
        self,
        event_id: str,
        tenant_id: str,
        version: int,
        *,
        action: str,
        actor: str,
        entry: dict | None = None,
    ) -> None:
        if action not in {"approve", "ignore", "retry_recognition"}:
            raise ValueError("invalid review action")
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT * FROM ingest_events WHERE id=? AND tenant_id=?",
                (event_id, tenant_id),
            ).fetchone()
            if row is None:
                raise KeyError("event not found")
            if row["version"] != version or row["status"] not in {
                "needs_review",
                "recognition_failed",
            }:
                raise StateConflict("review is stale or event is already resolved")
            if connection.execute(
                "SELECT 1 FROM delivery_operations WHERE event_id=?", (event_id,)
            ).fetchone():
                raise StateConflict("event already has a delivery operation")
            if action == "retry_recognition" and row["status"] != "recognition_failed":
                raise StateConflict("only failed recognition can be retried")
            before = connection.execute(
                "SELECT entry_json FROM recognition_results WHERE event_id=?",
                (event_id,),
            ).fetchone()
            before_json = before["entry_json"] if before else "{}"
            status = {
                "approve": "recognized",
                "ignore": "ignored",
                "retry_recognition": "queued",
            }[action]
            if action == "approve":
                if entry is None:
                    raise ValueError("approval requires a validated entry")
                connection.execute(
                    """INSERT INTO recognition_results(event_id,recognizer,entry_json,status,created_at)
                    VALUES(?,'human',?,'recognized',?) ON CONFLICT(event_id) DO UPDATE SET entry_json=excluded.entry_json,status='recognized' """,
                    (event_id, _json(entry), utc_now()),
                )
            elif before and action == "ignore":
                connection.execute(
                    "UPDATE recognition_results SET status='ignored' WHERE event_id=?",
                    (event_id,),
                )
            connection.execute(
                "UPDATE ingest_events SET status=?,last_error=NULL,lease_token=NULL,lease_until=NULL,version=version+1,updated_at=? WHERE id=?",
                (status, utc_now(), event_id),
            )
            connection.execute(
                "INSERT INTO review_history(event_id,action,actor,before_json,after_json,created_at) VALUES(?,?,?,?,?,?)",
                (event_id, action, actor, before_json, _json(entry or {}), utc_now()),
            )

    def confirm_reconciliation(
        self, event_id: str, operation_id: str, reference: str
    ) -> bool:
        """Called only after a sink has proved the immutable operation payload exists."""
        with self.transaction(immediate=True) as connection:
            row = connection.execute(
                "SELECT state FROM delivery_operations WHERE id=? AND event_id=?",
                (operation_id, event_id),
            ).fetchone()
            if row is None or row["state"] not in {"unknown", "confirmed"}:
                return False
            if row["state"] == "confirmed":
                return True
            now = utc_now()
            connection.execute(
                "UPDATE delivery_operations SET state='confirmed',external_ref=?,last_error=NULL,updated_at=? WHERE id=? AND state='unknown'",
                (reference, now, operation_id),
            )
            connection.execute(
                "UPDATE ingest_events SET status='completed',last_error=NULL,version=version+1,updated_at=? WHERE id=?",
                (now, event_id),
            )
            return True

    def record_ingress_error(
        self, source_kind: str, source_id: str, error_code: str
    ) -> None:
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO ingress_errors(source_kind,source_id,error_code,created_at) VALUES(?,?,?,?) ON CONFLICT(source_kind,source_id) DO UPDATE SET error_code=excluded.error_code,created_at=excluded.created_at",
                (source_kind, source_id, error_code, utc_now()),
            )

    def backup(self, destination: Path) -> None:
        if destination.exists() or destination.resolve() == self.path.resolve():
            raise ValueError("backup destination must be a new path")
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        descriptor = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        os.close(descriptor)
        with self.session() as source:
            target = sqlite3.connect(destination)
            try:
                source.backup(target)
            finally:
                target.close()


def _event_dict(row: sqlite3.Row) -> dict:
    event = dict(row)
    event["metadata"] = json.loads(event.pop("metadata_json"))
    return event


def public_event(event: dict) -> dict:
    """Explicit projection: adapter internals, paths and raw errors never reach clients."""
    result = {
        key: event[key]
        for key in (
            "id",
            "filename",
            "media_type",
            "status",
            "version",
            "created_at",
            "updated_at",
        )
    }
    if "recognition" in event:
        recognition = event["recognition"]
        result["recognition"] = {
            key: recognition[key]
            for key in ("recognizer", "entry", "status", "created_at")
        }
    if "delivery" in event:
        result["delivery"] = {
            key: event["delivery"][key]
            for key in ("id", "sink", "state", "created_at", "updated_at")
        }
    result["reviews"] = [
        {key: row[key] for key in ("action", "actor", "created_at")}
        for row in event["reviews"]
    ]
    return result
