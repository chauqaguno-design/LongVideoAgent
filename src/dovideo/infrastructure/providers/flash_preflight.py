"""Explicit, bounded capability preflight for the VideoMind Flash deployment.

Run without --live to inspect the saved report without contacting a provider.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
from datetime import datetime, timezone
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import re
from secrets import choice, token_hex
import shutil
from tempfile import NamedTemporaryFile

from PIL import Image

from .config import ProviderConfig, ProviderConfigurationError
from .http import StdlibAsyncJsonPostClient, response_parts

MODEL = "deepseek-flash"
FLASH_ID = re.compile(r"(?:deepseek(?:-ai)?/)?deepseek-(?:v4(?:\.1)?-)?flash(?:-\d{4,8})?", re.I)
ADAPTER = "chat-completions-flash-probe-v1"
CHECKS = ("text", "image", "image_json", "native_loop", "json_loop", "usage", "error")
JSON_FORMAT = {"response_format": {"type": "json_object"}}


def flash_config(env):
    # Scope credentials to the app's provider, never an unrelated SDK fallback.
    values = {k: v for k, v in env.items() if k.startswith("DOVIDEO_")}
    config = ProviderConfig.from_environment(values, required=True)
    if not FLASH_ID.fullmatch(config.model):
        raise ProviderConfigurationError("Configure a DeepSeek Flash deployment")
    if not config.api_key or config.api_key == "replace-me":
        raise ProviderConfigurationError("Configure the application provider credential")
    return config


def _fingerprint(config):
    identity = [ADAPTER, config.base_url, config.model, config.transport,
                config.provider_only, config.provider_data_collection, config.provider_zdr]
    return sha256(json.dumps(identity).encode()).hexdigest()


def _report(config=None):
    return dict(adapter=ADAPTER, fingerprint=_fingerprint(config) if config else None,
                general_model=MODEL, resolved_model_version="unverified",
                checked_at=datetime.now(timezone.utc).isoformat(), status="BLOCKED",
                calls=0, total_tokens=0, checks=dict.fromkeys(CHECKS, False), issues={})


class ProbeError(ValueError):
    """Only bounded, predefined failure codes are persisted."""


def _message(text):
    return {"role": "user", "content": text}


def _image_challenge():
    name = choice(("red", "green", "blue", "yellow"))
    image = Image.new("RGB", (64, 64), color=name)
    with BytesIO() as buf:
        image.save(buf, format="PNG")
        data = base64.b64encode(buf.getvalue()).decode()
    return name, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}}


def _check(condition):
    if not condition:
        raise ProbeError("unexpected_response")


async def run_preflight(config, client):
    report = _report(config)
    usage_ok = True
    stopped = False

    async def post(messages, *, invalid=False, **options):
        nonlocal usage_ok, stopped
        if stopped:
            raise ProbeError("provider_unavailable")
        payload = dict(model=config.model, max_tokens=128, stream=False, **options)
        if messages is not None:
            payload["messages"] = messages
        if config.transport == "openrouter":
            payload["provider"] = dict(only=list(config.provider_only), allow_fallbacks=False,
                                       require_parameters=True, data_collection="deny", zdr=True)
        report["calls"] += 1
        raw = await client.post(config.chat_url, headers={"Authorization": f"Bearer {config.api_key}"},
                                json=payload, timeout=min(15, config.timeout_seconds))
        status, body = response_parts(raw)
        if invalid and status in (400, 422):
            return None
        if status != 200:
            stopped = status in (401, 403, 429)
            raise ProbeError(f"http_{status}")
        _check(not invalid)
        resolved = body.get("model", "")
        if not FLASH_ID.fullmatch(resolved):
            stopped = True
            raise ProbeError("unexpected_model")
        if report["resolved_model_version"] not in ("unverified", resolved):
            stopped = True
            raise ProbeError("inconsistent_model")
        report["resolved_model_version"] = resolved
        usage = body.get("usage") or {}
        tokens = usage.get("total_tokens")
        if type(tokens) is int and tokens > 0:
            report["total_tokens"] += tokens
        else:
            usage_ok = False
        return body["choices"][0]["message"]

    async def probe(check):
        if check == "error":
            await post(None, invalid=True)
            return
        if check == "text":
            nonce = token_hex(4)
            reply = await post([_message("Return only this text: " + nonce)])
            _check(reply["content"].strip() == nonce)
            return
        if check in ("image", "image_json"):
            color, img = _image_challenge()
            instruction = ("Give only one English color word." if check == "image" else
                           'Return JSON with one string field "color".')
            data = [{"type": "text", "text": "What is the image's solid color? " + instruction}, img]
            options = JSON_FORMAT if check == "image_json" else {}
            answer = (await post([_message(data)], **options))["content"]
            actual = json.loads(answer)["color"] if options else answer
            _check(actual.strip().lower() == color)
            return

        # Native and JSON protocols perform the same harmless read. The receipt is
        # generated only AFTER a well-formed call, so it cannot be predicted up front.
        name = "read_sample"
        args = {"slot": "sample"}
        receipt = token_hex(8)
        if check == "native_loop":
            tool = {"type": "function", "function": {"name": name, "description": "Read sample receipt",
                    "parameters": {"type": "object", "properties": {"slot": {"type": "string"}},
                                   "required": ["slot"], "additionalProperties": False}}}
            msg = [_message("Call read_sample with slot=sample. Return only the receipt value you receive.")]
            reply = await post(msg, tools=[tool])
            calls = reply["tool_calls"]
            _check(len(calls) == 1 and calls[0]["function"]["name"] == name
                   and json.loads(calls[0]["function"]["arguments"]) == args and calls[0]["id"])
            result = {"role": "tool", "tool_call_id": calls[0]["id"], "content": receipt}
            # Retain all assistant wire fields (including reasoning_content) for this call only.
            final = await post([*msg, reply, result], tools=[tool])
            _check(final["content"].strip() == receipt)
        else:
            request = {"type": "TOOL_REQUEST", "name": name, "arguments": args}
            msg = [_message("Return this JSON tool request exactly. After the tool response, "
                            'return {"type":"FINAL","receipt":"<value>"}. ' + json.dumps(request))]
            reply = await post(msg, **JSON_FORMAT)
            _check(json.loads(reply["content"]) == request)
            result = _message(json.dumps({"type": "TOOL_RESULT", "name": name, "receipt": receipt}))
            final = await post([*msg, reply, result], **JSON_FORMAT)
            _check(json.loads(final["content"]) == {"type":"FINAL","receipt":receipt})

    for check in CHECKS:
        if check == "usage":
            continue
        try:
            await probe(check)
            report["checks"][check] = True
        except ProbeError as exc:
            report["issues"][check] = str(exc)
        except (OSError, TimeoutError):
            report["issues"][check] = "transport_failure"
            stopped = True
        except (KeyError, TypeError, IndexError, ValueError, AttributeError):
            report["issues"][check] = "malformed_response"
    report["checks"]["usage"] = usage_ok and report["total_tokens"] > 0
    if not report["checks"]["usage"]:
        report["issues"]["usage"] = "no_valid_usage"
        if report["calls"]:
            report["total_tokens"] = None  # A missing usage record is not a measured zero.
    if any(v in ("unexpected_model", "inconsistent_model") for v in report["issues"].values()):
        report["checks"] = dict.fromkeys(CHECKS, False)
    report["status"] = "PASSED" if all(report["checks"].values()) else "BLOCKED"
    return report


def report_path(env):
    return Path(env.get("DOVIDEO_CAPABILITIES_REPORT", "work/capabilities/flash.json"))


def current_report(env):
    try:
        config = flash_config(env)
    except ProviderConfigurationError:
        report = _report()
        report["issues"]["config"] = "flash_endpoint_model_or_credential_required"
        return report
    result = _report(config)
    path = report_path(env)
    if path.is_file():
        try:
            saved = json.loads(path.read_text(encoding="utf8"))
            if (saved["fingerprint"] == result["fingerprint"] and saved["adapter"] == ADAPTER
                    and set(saved["checks"]) == set(CHECKS)
                    and all(type(v) is bool for v in saved["checks"].values())):
                return saved
        except (OSError, KeyError, ValueError, TypeError):
            pass
    result["issues"]["preflight"] = "matching_live_report_required"
    return result


def capabilities(env):
    report = current_report(env)
    checks = report["checks"]
    ffmpeg = env.get("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=env.get("DOVIDEO_FFMPEG_DIR"))
    services = dict(ASR=env.get("DOVIDEO_WHISPER_DIR") or env.get("DOVIDEO_WHISPER_MODEL_ROOT") or env.get("DOVIDEO_ASR_API_KEY"),
                    OCR=env.get("DOVIDEO_PADDLE_OCR_DIR") or env.get("DOVIDEO_OCR_COMMAND") or shutil.which("tesseract"),
                    embedding=env.get("VIDEOMIND_SEARCH_ENCODER_URL") or env.get("DOVIDEO_EMBEDDING_BASE_URL"),
                    reranker=env.get("DOVIDEO_RERANKER_ENABLED", "").lower()=="true", FFmpeg=ffmpeg)
    return dict(general_model=MODEL, resolved_model_version=report["resolved_model_version"],
                checked_at=report["checked_at"], text=checks["text"],
                vision=checks["image"] and checks["image_json"], native_tools=checks["native_loop"],
                structured_json=checks["json_loop"], dedicated_services=[
                    dict(name=name, configured=bool(value), verified=False, version=None)
                    for name, value in services.items()])


def save_report(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", dir=path.parent, encoding="utf8", delete=False) as out:
        json.dump(report, out, indent=2, ensure_ascii=False)
        out.write("\n")
    Path(out.name).replace(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send up to eight billed, bounded requests")
    args = parser.parse_args()
    if args.live:
        try:
            config = flash_config(os.environ)
        except ProviderConfigurationError:
            report = current_report(os.environ)
        else:
            report = asyncio.run(run_preflight(config, StdlibAsyncJsonPostClient()))
        save_report(report_path(os.environ), report)
    else:
        report = current_report(os.environ)
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "PASSED" else 2


if __name__ == "__main__":
    raise SystemExit(main())
