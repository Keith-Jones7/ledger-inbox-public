from __future__ import annotations

import base64
import importlib
import json
from dataclasses import replace
from typing import Any

import httpx

from ..config import Settings, use_environment_proxy
from ..domain import LedgerEntry, RecognitionContext, Recognizer
from ..image_store import model_image
from ..policy import policy_contract, prompt


class MockRecognizer:
    name = "mock"

    def recognize(self, context: RecognitionContext) -> LedgerEntry:
        return LedgerEntry(
            "unknown",
            None,
            "",
            None,
            context.captured_at,
            None,
            confidence=0,
            needs_review=True,
            review_reasons=("mock_recognizer",),
        ).validated()


class OpenAICompatibleRecognizer:
    name = "openai-compatible"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.last_audit: dict = {}

    def _extract(
        self, context: RecognitionContext, model: str
    ) -> tuple[LedgerEntry, str]:
        encoded = base64.b64encode(model_image(context.image_path)).decode("ascii")
        contract = policy_contract(self.settings.categories)
        payload = {
            "model": model,
            "temperature": 0,
            "max_tokens": 1024,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "ledger_entry",
                    "strict": True,
                    "schema": contract["schema"],
                },
            },
            "messages": [
                {"role": "system", "content": prompt(self.settings.categories)},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Extract the actual payment shown in this image. If timezone is not established, require review.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/png;base64,{encoded}"},
                        },
                    ],
                },
            ],
        }
        # Ollama-native thinking must be disabled for predictable small-model budgets.
        if self.settings.model_base_url.endswith(":11434/v1"):
            payload["reasoning_effort"] = "none"
        response = httpx.post(
            f"{self.settings.model_base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.settings.model_api_key}"},
            json=payload,
            timeout=self.settings.model_timeout_seconds,
            follow_redirects=False,
            trust_env=use_environment_proxy(self.settings.model_base_url),
        )
        response.raise_for_status()
        content = response.json()["choices"][0]["message"]["content"]
        if not isinstance(content, str) or len(content) > 65536:
            raise ValueError("model content is invalid or too large")
        return LedgerEntry.from_dict(
            _parse_json_object(content), **self.settings.validation
        ), content

    def recognize(self, context: RecognitionContext) -> LedgerEntry:
        self.last_audit = {
            "policy": policy_contract(self.settings.categories),
            "primary_model": self.settings.model_name,
        }
        primary, raw = self._extract(context, self.settings.model_name)
        self.last_audit["primary_raw"] = raw
        if not self.settings.shadow_model:
            return primary
        try:
            shadow, raw = self._extract(context, self.settings.shadow_model)
            self.last_audit.update(
                shadow_model=self.settings.shadow_model, shadow_raw=raw
            )
            fields = (
                "amount_minor",
                "currency",
                "transaction_type",
                "occurred_at",
                "category",
            )
            conflicts = [
                f"shadow_conflict_{field}"
                for field in fields
                if getattr(primary, field) != getattr(shadow, field)
            ]
            if shadow.needs_review:
                conflicts.append("shadow_requires_review")
        except Exception:
            conflicts = ["shadow_unavailable"]
            self.last_audit["shadow_error"] = "shadow_unavailable"
        if conflicts:
            primary = replace(
                primary,
                needs_review=True,
                review_reasons=tuple(
                    dict.fromkeys((*primary.review_reasons, *conflicts))
                ),
            )
        return primary


def load_recognizer(spec: str, settings: Settings) -> Recognizer:
    if spec == "mock":
        return MockRecognizer()
    if spec in {"openai", "openai-compatible"}:
        return OpenAICompatibleRecognizer(settings)
    loaded = _load_object(spec)
    instance = loaded(settings) if isinstance(loaded, type) else loaded
    if not hasattr(instance, "recognize") or not hasattr(instance, "name"):
        raise TypeError("recognizer must expose name and recognize(context)")
    return instance


def _load_object(spec: str) -> Any:
    if ":" not in spec:
        raise ValueError("custom adapter must use module.path:object format")
    module, name = spec.split(":", 1)
    return getattr(importlib.import_module(module), name)


def _parse_json_object(content: str) -> dict:
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = (
            "\n".join(lines[1:-1]).strip()
            if lines[-1].strip() == "```"
            else "\n".join(lines[1:]).strip()
        )
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("model output must be one JSON object")
    return value
