from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    return default if raw is None else raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_dir: Path = Path("data")
    database_path: Path | None = None
    max_image_bytes: int = 12 * 1024 * 1024
    max_email_bytes: int = 48 * 1024 * 1024
    max_image_pixels: int = 25_000_000
    webhook_secret: str | None = None
    recognizer: str = "mock"
    ledger_sink: str = "directory"
    model_base_url: str = "http://127.0.0.1:11434/v1"
    model_api_key: str = "local"
    model_name: str = "qwen3.5:4b"
    model_timeout_seconds: float = 90.0
    shadow_model: str | None = None
    min_confidence: float = 0.9
    required_fields: tuple[str, ...] = ("occurred_at", "category")
    categories: tuple[str, ...] = ()
    lease_seconds: int = 600
    max_recognition_attempts: int = 3
    ledger_webhook_url: str | None = None
    ledger_webhook_secret: str | None = None
    ledger_status_url: str | None = None
    trust_proxy_headers: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.min_confidence <= 1:
            raise ValueError("confidence threshold must be between 0 and 1")
        if not set(self.required_fields) <= {
            "occurred_at",
            "category",
            "merchant",
            "account",
        }:
            raise ValueError("unsupported required field")
        if (
            min(
                self.max_image_bytes,
                self.max_email_bytes,
                self.max_image_pixels,
                self.lease_seconds,
                self.max_recognition_attempts,
            )
            <= 0
        ):
            raise ValueError("limits must be positive")
        if not 0 < self.model_timeout_seconds < self.lease_seconds / 2:
            raise ValueError(
                "model timeout must be positive and below half the worker lease"
            )
        for url in (
            self.model_base_url,
            self.ledger_webhook_url,
            self.ledger_status_url,
        ):
            if url:
                parsed = urlparse(url)
                if (
                    parsed.username
                    or parsed.password
                    or parsed.scheme not in {"http", "https"}
                    or not parsed.hostname
                ):
                    raise ValueError(
                        "endpoint must be HTTP(S) without embedded credentials"
                    )
                if parsed.scheme == "http" and parsed.hostname not in {
                    "localhost",
                    "127.0.0.1",
                    "::1",
                    "host.docker.internal",
                }:
                    raise ValueError("remote endpoints require HTTPS")

    @property
    def db_path(self) -> Path:
        return self.database_path or self.data_dir / "ledger-inbox.sqlite3"

    @property
    def validation(self) -> dict:
        return {
            "confidence_threshold": self.min_confidence,
            "required_fields": self.required_fields,
            "categories": self.categories,
        }

    @classmethod
    def from_env(cls) -> Settings:
        def setting(name: str, default: str = "") -> str:
            return os.getenv(f"LEDGER_INBOX_{name}", default)

        def optional(name: str) -> str | None:
            return setting(name) or None

        def csv(name: str, default: str = "") -> tuple[str, ...]:
            return tuple(
                x.strip() for x in setting(name, default).split(",") if x.strip()
            )

        return cls(
            data_dir=Path(setting("DATA_DIR", "data")).expanduser(),
            database_path=Path(setting("DATABASE_PATH")).expanduser()
            if setting("DATABASE_PATH")
            else None,
            max_image_bytes=int(setting("MAX_IMAGE_BYTES", "12582912")),
            max_email_bytes=int(setting("MAX_EMAIL_BYTES", "50331648")),
            max_image_pixels=int(setting("MAX_IMAGE_PIXELS", "25000000")),
            webhook_secret=optional("EMAIL_WEBHOOK_SECRET"),
            recognizer=setting("RECOGNIZER", "mock"),
            ledger_sink=setting("LEDGER_SINK", "directory"),
            model_base_url=setting(
                "MODEL_BASE_URL", "http://127.0.0.1:11434/v1"
            ).rstrip("/"),
            model_api_key=setting("MODEL_API_KEY", "local"),
            model_name=setting("MODEL_NAME", "qwen3.5:4b"),
            model_timeout_seconds=float(setting("MODEL_TIMEOUT_SECONDS", "90")),
            shadow_model=optional("SHADOW_MODEL"),
            min_confidence=float(setting("MIN_CONFIDENCE", "0.9")),
            categories=csv("CATEGORIES"),
            required_fields=csv("REQUIRED_FIELDS", "occurred_at,category"),
            lease_seconds=int(setting("LEASE_SECONDS", "600")),
            max_recognition_attempts=int(setting("MAX_RECOGNITION_ATTEMPTS", "3")),
            ledger_webhook_url=optional("LEDGER_WEBHOOK_URL"),
            ledger_webhook_secret=optional("LEDGER_WEBHOOK_SECRET"),
            ledger_status_url=optional("LEDGER_STATUS_URL"),
            trust_proxy_headers=_env_bool("LEDGER_INBOX_TRUST_PROXY_HEADERS", False),
        )


def use_environment_proxy(url: str) -> bool:
    """Never send local model/ledger requests through an ambient proxy."""
    return urlparse(url).hostname not in {
        "localhost",
        "127.0.0.1",
        "::1",
        "host.docker.internal",
    }
