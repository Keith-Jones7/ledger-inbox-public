from __future__ import annotations

import math
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any, Literal, Protocol

from iso4217 import Currency

TransactionType = Literal["expense", "income", "refund", "transfer", "unknown"]


def valid_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or "T" not in value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.utcoffset() is not None
    except (ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class RecognitionContext:
    event_id: str
    tenant_id: str
    image_path: str
    media_type: str
    filename: str
    captured_at: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True)
class LedgerEntry:
    transaction_type: TransactionType
    amount_minor: int | None
    currency: str
    merchant: str | None
    occurred_at: str | None
    category: str | None
    account: str | None = None
    note: str | None = None
    confidence: float | None = None
    needs_review: bool = False
    review_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["review_reasons"] = list(self.review_reasons)
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any], **validation: Any) -> LedgerEntry:
        if not isinstance(payload, dict):
            raise ValueError("entry must be an object")
        reasons = payload.get("review_reasons", [])
        if not isinstance(reasons, (list, tuple)) or any(
            not isinstance(x, str) for x in reasons
        ):
            raise ValueError("review_reasons must be an array of strings")
        review = payload.get("needs_review", False)
        if not isinstance(review, bool):
            raise ValueError("needs_review must be boolean")
        amount = payload.get("amount_minor")
        invalid = []
        if amount is not None and (
            type(amount) is not int or not 0 < amount <= 2**63 - 1
        ):
            amount = None
            invalid.append("invalid_or_missing_amount")
        confidence = payload.get("confidence")
        if confidence is not None and (
            type(confidence) not in (int, float) or not math.isfinite(confidence)
        ):
            confidence = None
            invalid.append("invalid_confidence")
        return cls(
            transaction_type=_optional_text(payload.get("transaction_type"))
            or "unknown",
            amount_minor=amount,
            currency=(_optional_text(payload.get("currency")) or "").upper(),
            merchant=_optional_text(payload.get("merchant")),
            occurred_at=_optional_text(payload.get("occurred_at")),
            category=_optional_text(payload.get("category")),
            account=_optional_text(payload.get("account")),
            note=_optional_text(payload.get("note")),
            confidence=confidence,
            needs_review=review,
            review_reasons=tuple(list(reasons) + invalid),
        ).validated(**validation)

    def validated(
        self,
        *,
        confidence_threshold: float = 0.9,
        required_fields: tuple[str, ...] = ("occurred_at", "category"),
        categories: tuple[str, ...] = (),
        human: bool = False,
    ) -> LedgerEntry:
        reasons = list(self.review_reasons)
        if self.transaction_type not in {"expense", "income", "refund", "transfer"}:
            reasons.append("unknown_transaction_type")
        if type(self.amount_minor) is not int or not 0 < self.amount_minor <= 2**63 - 1:
            reasons.append("invalid_or_missing_amount")
        try:
            currency = Currency(self.currency)
            if currency.exponent is None:
                reasons.append("unsupported_currency_minor_units")
        except (ValueError, TypeError):
            reasons.append("invalid_currency")
        if self.occurred_at is not None and not valid_timestamp(self.occurred_at):
            reasons.append("invalid_occurred_at")
        for field in required_fields:
            if not getattr(self, field, None):
                reasons.append(f"missing_{field}")
        if categories and self.category not in categories:
            reasons.append("invalid_category")
        if not human:
            if (
                type(self.confidence) not in (int, float)
                or not math.isfinite(self.confidence)
                or not 0 <= self.confidence <= 1
            ):
                reasons.append("invalid_or_missing_confidence")
            elif self.confidence < confidence_threshold:
                reasons.append("low_confidence")
        reasons = tuple(dict.fromkeys(reasons))
        return replace(
            self,
            needs_review=self.needs_review or bool(reasons),
            review_reasons=reasons,
        )


class Recognizer(Protocol):
    name: str

    def recognize(self, context: RecognitionContext) -> LedgerEntry: ...


class LedgerSink(Protocol):
    name: str

    def deliver(
        self, entry: LedgerEntry, *, event_id: str, operation_id: str
    ) -> str | None: ...


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("text fields must be strings or null")
    text = value.strip()
    if len(text) > 4096:
        raise ValueError("text field exceeds limit")
    return text or None
