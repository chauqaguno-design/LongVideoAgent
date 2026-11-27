"""Business keys survive a new lease. Only a new plan changes their generation."""

from contextvars import ContextVar
from hashlib import sha256
import json
from math import ceil
import os


COMPLETION_TOKENS = 1024
reserved_completion_tokens: ContextVar[int | None] = ContextVar("reserved_completion_tokens", default=None)


def completion_tokens_for(phase=None, env=None) -> int:
    env = os.environ if env is None else env
    if phase == "PLAN":
        setting, default = "CUTSIEVE_PLAN_MAX_TOKENS", 1536
    else:
        setting, default = "CUTSIEVE_COMPLETION_TOKENS", 768
    try:
        return max(256, int(env.get(setting, default)))
    except (TypeError, ValueError):
        return default


def digest_json(value):
    return sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def context_estimate(messages, tools, *, phase=None, env=None):
    """Estimate text by script and allow 2,048 tokens per supplied image."""
    images = 0
    projected = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            parts = []
            for part in content:
                if part.get("type") == "image_url":
                    images += 1
                    parts.append({"type": "image_url"})
                else:
                    parts.append(part)
            message = dict(message, content=parts)
        projected.append(message)
    serialized = json.dumps([projected, tools], ensure_ascii=False)
    ascii_chars = sum(char.isascii() for char in serialized)
    non_ascii_chars = len(serialized) - ascii_chars
    text_tokens = ceil((non_ascii_chars * 1.2 + ascii_chars / 3.5) * 1.15)
    return dict(
        input_tokens=text_tokens + 2048 * images,
        images=images,
        completion_tokens=completion_tokens_for(phase, env),
    )


def generation_key(checkpoint, key):
    return f"{key}:g{checkpoint.attempt.plan_generation}"


def workflow_version(checkpoint):
    return dict(
        turn=checkpoint.attempt.turn,
        plan_generation=checkpoint.attempt.plan_generation,
        repair_count=checkpoint.repair.repair_count,
    )
