from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, ConfigDict, Field, StrictInt

from . import __version__
from .adapters.ledgers import load_ledger_sink
from .adapters.recognizers import MockRecognizer
from .config import Settings
from .database import Database, StateConflict, public_event
from .image_store import ImageStore
from .ingestion import IngestionService
from .limits import UploadLimitMiddleware
from .review import apply_review, review_contract
from .security import verify_webhook_signature
from .worker import Worker


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: Annotated[StrictInt, Field(ge=1)]
    action: Literal["approve", "ignore", "retry_recognition"]
    entry: dict | None = None


async def bounded_body(request: Request, limit: int) -> bytes:
    length = request.headers.get("content-length")
    if length:
        try:
            if int(length) > limit:
                raise HTTPException(413, "request is too large")
        except ValueError:
            raise HTTPException(400, "invalid content length") from None
    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > limit:
            raise HTTPException(413, "request is too large")
        body.extend(chunk)
    return bytes(body)


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings or Settings.from_env()
    database = Database(config.db_path)
    database.initialize()
    ingestion = IngestionService(
        database,
        ImageStore(config.data_dir / "images", config.max_image_pixels),
        config.max_image_bytes,
        config.max_email_bytes,
    )
    app = FastAPI(
        title="Ledger Inbox",
        version=__version__,
        description="Multi-channel bookkeeping with durable review and delivery.",
    )
    app.add_middleware(UploadLimitMiddleware, max_bytes=config.max_image_bytes + 65536)
    app.state.settings, app.state.database, app.state.ingestion = (
        config,
        database,
        ingestion,
    )

    @app.middleware("http")
    async def private_responses(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    def tenant_from_key(
        x_ledger_inbox_key: Annotated[str | None, Header()] = None,
    ) -> dict:
        tenant = (
            database.tenant_for_api_key(x_ledger_inbox_key)
            if x_ledger_inbox_key
            else None
        )
        if tenant is None:
            raise HTTPException(401, "missing or invalid API key")
        return tenant

    def tenant_event(event_id: str, tenant: dict) -> dict:
        event = database.get_event(event_id, tenant["id"])
        if event is None:
            raise HTTPException(404, "event not found")
        return event

    @app.get("/healthz", tags=["operations"])
    def health() -> dict:
        with database.session() as connection:
            connection.execute("SELECT 1 FROM ingest_events LIMIT 1")
        return {"status": "ok", "version": __version__}

    @app.post("/v1/ingest/images", status_code=202, tags=["ingestion"])
    async def ingest_image(
        image: Annotated[UploadFile, File()],
        tenant: dict = Depends(tenant_from_key),
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        x_captured_at: Annotated[str | None, Header()] = None,
    ) -> dict:
        body = await image.read(config.max_image_bytes + 1)
        try:
            return ingestion.ingest_image(
                tenant_id=tenant["id"],
                image=body,
                filename=image.filename or "image",
                source_kind="https",
                source_event_id=idempotency_key,
                captured_at=x_captured_at,
            ).to_dict()
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            await image.close()

    @app.post("/v1/inbound/email/rfc822", status_code=202, tags=["ingestion"])
    async def ingest_rfc822(
        request: Request,
        x_ledger_inbox_recipient: Annotated[str | None, Header()] = None,
        x_ledger_inbox_timestamp: Annotated[str | None, Header()] = None,
        x_ledger_inbox_signature: Annotated[str | None, Header()] = None,
    ) -> dict:
        if not config.webhook_secret:
            raise HTTPException(503, "email webhook is disabled")
        if not all(
            (
                x_ledger_inbox_recipient,
                x_ledger_inbox_timestamp,
                x_ledger_inbox_signature,
            )
        ):
            raise HTTPException(401, "missing webhook authentication")
        body = await bounded_body(request, config.max_email_bytes)
        if not verify_webhook_signature(
            config.webhook_secret,
            x_ledger_inbox_timestamp,
            x_ledger_inbox_recipient,
            body,
            x_ledger_inbox_signature,
        ):
            raise HTTPException(401, "invalid webhook signature")
        tenant = database.tenant_for_alias(x_ledger_inbox_recipient)
        if tenant is None:
            raise HTTPException(404, "unknown inbound alias")
        try:
            # Only authenticated bytes determine identity; unsigned forwarding headers cannot override it.
            results = ingestion.ingest_email(
                tenant_id=tenant["id"],
                raw_message=body,
                recipient=x_ledger_inbox_recipient,
            )
            return {"events": [result.to_dict() for result in results]}
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/v1/events", tags=["events"])
    def list_events(
        status: str = "needs_review", tenant: dict = Depends(tenant_from_key)
    ) -> dict:
        return {"events": database.list_events(tenant["id"], status)}

    @app.get("/v1/events/{event_id}", tags=["events"])
    def get_event(event_id: str, tenant: dict = Depends(tenant_from_key)) -> dict:
        return public_event(tenant_event(event_id, tenant))

    @app.get("/v1/events/{event_id}/image", tags=["review"])
    def get_image(event_id: str, tenant: dict = Depends(tenant_from_key)) -> Response:
        from pathlib import Path

        event = tenant_event(event_id, tenant)
        try:
            body = Path(event["image_path"]).read_bytes()
        except OSError:
            raise HTTPException(409, "original image is unavailable") from None
        if hashlib.sha256(body).hexdigest() != event["content_sha256"]:
            raise HTTPException(409, "original image integrity failure")
        return Response(
            body,
            media_type=event["media_type"],
            headers={"Content-Disposition": "inline"},
        )

    @app.get("/v1/review/contract", tags=["review"])
    def get_review_contract(tenant: dict = Depends(tenant_from_key)) -> dict:
        return review_contract(config)

    @app.post("/v1/events/{event_id}/review", tags=["review"])
    def review(
        event_id: str, request: ReviewRequest, tenant: dict = Depends(tenant_from_key)
    ) -> dict:
        tenant_event(event_id, tenant)
        try:
            apply_review(
                database,
                config,
                event_id,
                tenant["id"],
                request.expected_version,
                request.action,
                request.entry,
            )
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        except KeyError:
            raise HTTPException(404, "event not found") from None
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return public_event(tenant_event(event_id, tenant))

    @app.post("/v1/events/{event_id}/reconcile", tags=["review"])
    def reconcile(event_id: str, tenant: dict = Depends(tenant_from_key)) -> dict:
        tenant_event(event_id, tenant)
        worker = Worker(
            database,
            MockRecognizer(),
            load_ledger_sink(config.ledger_sink, config),
            config,
        )
        try:
            worker.reconcile(event_id, tenant["id"])
        except StateConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return public_event(tenant_event(event_id, tenant))

    return app
