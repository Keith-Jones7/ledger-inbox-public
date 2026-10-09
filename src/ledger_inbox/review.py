from __future__ import annotations

from .config import Settings
from .database import Database
from .domain import LedgerEntry
from .policy import policy_contract


def apply_review(
    database: Database,
    settings: Settings,
    event_id: str,
    tenant_id: str,
    version: int,
    action: str,
    payload: dict | None,
) -> None:
    entry = None
    if action == "approve":
        if payload is None:
            raise ValueError("approval requires the complete corrected entry")
        entry = LedgerEntry.from_dict(payload, human=True, **settings.validation)
        if entry.needs_review:
            raise ValueError(
                "entry still requires review: " + ",".join(entry.review_reasons)
            )
    database.review_event(
        event_id,
        tenant_id,
        version,
        action=action,
        actor=f"tenant:{tenant_id}",
        entry=entry.to_dict() if entry else None,
    )


def review_contract(settings: Settings) -> dict:
    return {
        "policy": policy_contract(settings.categories),
        "required_fields": settings.required_fields,
        "categories": settings.categories,
        "min_confidence": settings.min_confidence,
    }
