"""OpenAI-compatible settings for the workflow model and visual observations."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Mapping
from urllib.parse import urlsplit


class ProviderConfigurationError(ValueError):
    """A provider setting is missing or outside its supported boundary."""


@dataclass(frozen=True, slots=True)
class ProviderConfig:
    """OpenAI-compatible chat deployment.

    ``api_key`` is optional to support local OpenAI-compatible servers.  A
    remote deployment can require it in its composition root without making
    credentials a domain concern.  The key is excluded from ``repr`` and is
    never included in adapter errors.
    """

    base_url: str
    model: str
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: float = 120.0
    max_attempts: int = 3
    retry_delay_seconds: float = 0.0
    transport: str = "openai-compatible"
    provider_only: tuple[str, ...] = ()
    provider_data_collection: str | None = None
    provider_zdr: bool | None = None
    response_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.base_url, str) or not self.base_url.strip():
            raise ProviderConfigurationError("provider base URL is required")
        base_url = self.base_url.strip().rstrip("/")
        # Accept a fully qualified chat/embedding endpoint as a convenience,
        # while keeping one normalized base for both OpenAI-compatible paths.
        for suffix in ("/chat/completions", "/embeddings"):
            if base_url.endswith(suffix):
                base_url = base_url[: -len(suffix)]
                break
        try:
            parsed = urlsplit(base_url)
        except ValueError as exc:
            raise ProviderConfigurationError("provider base URL must be HTTP(S)") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ProviderConfigurationError("provider base URL must be HTTP(S)")
        if not isinstance(self.model, str) or not self.model.strip():
            raise ProviderConfigurationError("provider model is required")
        if self.api_key is not None:
            if not isinstance(self.api_key, str):
                raise ProviderConfigurationError("provider api key must be text")
            api_key = self.api_key.strip() or None
        else:
            api_key = None
        timeout = _finite_positive(self.timeout_seconds, "timeout_seconds")
        attempts = _positive_int(self.max_attempts, "max_attempts")
        delay = _finite_nonnegative(self.retry_delay_seconds, "retry_delay_seconds")
        if self.transport not in {"openai-compatible", "openrouter"}:
            raise ProviderConfigurationError("unsupported model transport")
        if self.transport == "openrouter":
            if base_url != "https://openrouter.ai/api/v1":
                raise ProviderConfigurationError("OpenRouter transport requires its API base URL")
            if not api_key:
                raise ProviderConfigurationError("OpenRouter API key is required")
            if not self.model.strip().startswith("deepseek/"):
                raise ProviderConfigurationError("DeepSeek execution requires an OpenRouter DeepSeek model slug")
            if not self.provider_only or any(not item.strip() for item in self.provider_only):
                raise ProviderConfigurationError("OpenRouter execution provider pin is required")
            if self.provider_data_collection != "deny" or self.provider_zdr is not True:
                raise ProviderConfigurationError("OpenRouter execution requires deny collection and ZDR")
        elif self.provider_only or self.provider_data_collection is not None or self.provider_zdr is not None:
            raise ProviderConfigurationError("provider policy requires OpenRouter transport")
        object.__setattr__(self, "base_url", base_url)
        object.__setattr__(self, "model", self.model.strip())
        object.__setattr__(self, "api_key", api_key)
        object.__setattr__(self, "timeout_seconds", timeout)
        object.__setattr__(self, "max_attempts", attempts)
        object.__setattr__(self, "retry_delay_seconds", delay)

    @property
    def chat_url(self) -> str:
        """OpenAI-compatible chat completion endpoint."""

        if self.base_url.endswith("/chat/completions"):
            return self.base_url
        return f"{self.base_url}/chat/completions"

    @property
    def loopback(self) -> bool:
        return urlsplit(self.base_url).hostname in {"localhost", "127.0.0.1", "::1"}

    @property
    def resolved_model(self) -> str:
        return self.response_model or self.model

    @property
    def auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}

    def request_options(self) -> dict:
        if self.transport == "openrouter":
            return {"provider": dict(
                only=list(self.provider_only), allow_fallbacks=False, require_parameters=True,
                data_collection=self.provider_data_collection, zdr=self.provider_zdr,
            )}
        if urlsplit(self.base_url).hostname == "api.deepseek.com":
            return {"thinking": {"type": "disabled"}}
        return {}

    def fingerprint(self) -> str:
        # Preserve the deployment fingerprint already pinned in workflow checkpoints.
        parts = ["chat-completions-flash-probe-v1", self.base_url, self.model, self.transport,
                 self.provider_only, self.provider_data_collection, self.provider_zdr]
        if thinking := self.request_options().get("thinking"):
            parts.append({"thinking": thinking})
        return sha256(json.dumps(parts).encode()).hexdigest()

    @classmethod
    def from_environment(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        prefix: str = "DOVIDEO_",
        required: bool = False,
    ) -> "ProviderConfig | None":
        """Build settings from environment without reading it at import time."""

        values = os.environ if environ is None else environ
        response_model = _first_value(values, f"{prefix}MODEL_VERSION")
        transport = _first_value(values, f"{prefix}MODEL_TRANSPORT") or "openai-compatible"
        if transport == "openrouter":
            key = _first_value(values, f"{prefix}OPENROUTER_API_KEY")
            provider_tag = _first_value(values, f"{prefix}MODEL_PROVIDER_TAG")
            if provider_tag is None:
                raise ProviderConfigurationError("OpenRouter execution provider tag is required")
            try:
                timeout = float(_first_value(values, f"{prefix}MODEL_TIMEOUT_SECONDS") or 120)
                attempts = int(_first_value(values, f"{prefix}MODEL_MAX_ATTEMPTS") or 3)
                delay = float(_first_value(values, f"{prefix}MODEL_RETRY_DELAY_SECONDS") or 0)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ProviderConfigurationError("provider numeric setting is invalid") from exc
            return cls(
                base_url="https://openrouter.ai/api/v1",
                model=_first_value(values, f"{prefix}BALANCED_MODEL", f"{prefix}MODEL_MODEL")
                or "deepseek/deepseek-v4.1-flash",
                api_key=key,
                transport="openrouter",
                provider_only=(provider_tag,),
                provider_data_collection="deny",
                provider_zdr=True,
                timeout_seconds=timeout,
                max_attempts=attempts,
                retry_delay_seconds=delay,
                response_model=response_model,
            )
        if transport != "openai-compatible":
            raise ProviderConfigurationError("unsupported model transport")
        endpoint = _first_value(
            values,
            f"{prefix}MODEL_BASE_URL",
            f"{prefix}BASE_URL",
            f"{prefix}DEEPSEEK_BASE_URL",
            "SILICONFLOW_BASE_URL",
        )
        if endpoint is None:
            if required:
                raise ProviderConfigurationError("provider base URL is required")
            return None
        model = _first_value(
            values,
            f"{prefix}MODEL",
            f"{prefix}MODEL_MODEL",
            f"{prefix}MODEL_NAME",
            f"{prefix}LLM_MODEL",
            "LLM_MODEL",
        ) or "deepseek-flash"
        api_key = _first_value(
            values,
            f"{prefix}MODEL_API_KEY",
            f"{prefix}API_KEY",
            f"{prefix}DEEPSEEK_API_KEY",
            "SILICONFLOW_API_KEY",
        )
        timeout_text = _first_value(
            values,
            f"{prefix}MODEL_TIMEOUT_SECONDS",
            f"{prefix}TIMEOUT_SECONDS",
            f"{prefix}LLM_TIMEOUT_SECONDS",
            "LLM_TIMEOUT_SECONDS",
        )
        attempts_text = _first_value(values, f"{prefix}MODEL_MAX_ATTEMPTS")
        delay_text = _first_value(values, f"{prefix}MODEL_RETRY_DELAY_SECONDS")
        try:
            timeout = float(timeout_text) if timeout_text is not None else 120.0
            attempts = int(attempts_text) if attempts_text is not None else 3
            delay = float(delay_text) if delay_text is not None else 0.0
        except (TypeError, ValueError, OverflowError) as exc:
            raise ProviderConfigurationError("provider numeric setting is invalid") from exc
        return cls(
            base_url=endpoint,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout,
            max_attempts=attempts,
            retry_delay_seconds=delay,
            response_model=response_model,
        )


def configured_model(env: Mapping[str, str]) -> ProviderConfig | None:
    """Read the app's deployment and credentials from its own environment variables."""
    values = {key: value for key, value in env.items() if key.startswith("DOVIDEO_")}
    try:
        config = ProviderConfig.from_environment(values)
    except ProviderConfigurationError:
        return None
    if config is None or config.api_key == "replace-me" or (not config.loopback and not config.api_key):
        return None
    return config


def model_supports_vision(env: Mapping[str, str]) -> bool:
    return env.get("DOVIDEO_MODEL_VISION", "1") == "1"


def model_tool_mode(env: Mapping[str, str]) -> str:
    return "native" if env.get("DOVIDEO_MODEL_NATIVE_TOOLS") == "1" else "json"


def _first_value(values: Mapping[str, str], *names: str) -> str | None:
    for name in names:
        value = values.get(name)
        if value is not None and value.strip():
            return value.strip()
    return None


def _finite_positive(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigurationError(f"{field_name} must be finite and positive")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ProviderConfigurationError(f"{field_name} must be finite and positive")
    return result


def _finite_nonnegative(value: object, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderConfigurationError(
            f"{field_name} must be finite and non-negative"
        )
    result = float(value)
    if not math.isfinite(result) or result < 0:
        raise ProviderConfigurationError(
            f"{field_name} must be finite and non-negative"
        )
    return result


def _positive_int(value: object, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProviderConfigurationError(f"{field_name} must be a positive integer")
    return value
