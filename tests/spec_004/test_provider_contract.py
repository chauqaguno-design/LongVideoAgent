"""Exercise the Flash wire contract with a deterministic in-memory transport."""

import base64
from io import BytesIO
import json

import pytest
from PIL import Image

from dovideo.infrastructure.providers.config import ProviderConfigurationError
from dovideo.infrastructure.providers.flash_preflight import (
    capabilities, current_report, flash_config, report_path, run_preflight, save_report,
)


@pytest.fixture
def env(tmp_path):
    return {"DOVIDEO_MODEL_BASE_URL": "https://flash.example.invalid/v1",
            "DOVIDEO_MODEL_MODEL": "deepseek-flash", "DOVIDEO_MODEL_API_KEY": "fixture-only-secret",
            "DOVIDEO_CAPABILITIES_REPORT": str(tmp_path / "saved.json")}


class EchoTransport:
    def __init__(self, *, fail_image=False, model="deepseek-flash", collect_usage=True):
        self.fail_image = fail_image
        self.model = model
        self.collect_usage = collect_usage

    async def post(self, url, *, headers, json: dict, timeout):
        import json as codec
        if "messages" not in json:
            return {"status_code": 400}
        messages = json["messages"]
        last = messages[-1]
        content = last["content"]
        if last["role"] == "tool":
            assert last["tool_call_id"] == messages[-2]["tool_calls"][0]["id"]
            assert messages[-2]["reasoning_content"] == "wire-only"
            reply = {"content": content}
        elif len(messages) > 1:
            receipt = codec.loads(content)["receipt"]
            reply = {"content": codec.dumps({"type":"FINAL","receipt":receipt})}
        elif isinstance(content,list):
            if self.fail_image:
                return {"status_code": 400, "body": {"error": headers["Authorization"]}}
            rgb = Image.open(BytesIO(base64.b64decode(content[1]["image_url"]["url"].split(",")[1]))).getpixel((0,0))
            color = {(255,0,0):"red", (0,128,0):"green", (0,0,255):"blue", (255,255,0):"yellow"}[rgb]
            reply = {"content": codec.dumps({"color":color}) if "response_format" in json else color}
        elif "tools" in json:
            reply = {"content": "", "reasoning_content": "wire-only", "tool_calls": [
                {"id": "call1", "type": "function", "function": {"name": "read_sample",
                 "arguments": '{"slot":"sample"}'}}]}
        elif "response_format" in json:
            reply = {"content": '{"type":"TOOL_REQUEST","name":"read_sample","arguments":{"slot":"sample"}}'}
        else:
            reply = {"content": content.split()[-1]}
        return {"model":self.model,"choices":[{"message":{"role":"assistant",**reply}}],
                "usage":{"total_tokens":10} if self.collect_usage else {}}


@pytest.mark.asyncio
async def test_verified_report_is_bound_to_deployment(env):
    config = flash_config(env)
    model_report = await run_preflight(config, EchoTransport())
    assert model_report["status"] == "PASSED"
    assert model_report["calls"] == 8
    assert model_report["total_tokens"] == 70
    saved = report_path(env)
    save_report(saved, model_report)
    result = capabilities(env)
    assert result["text"] and result["native_tools"] and result["vision"] and result["structured_json"]
    assert env["DOVIDEO_MODEL_API_KEY"] not in saved.read_text()
    assert "wire-only" not in saved.read_text()
    assert "data:image" not in saved.read_text()
    env["DOVIDEO_MODEL_BASE_URL"] = "https://changed.invalid"
    assert not capabilities(env)["text"]


@pytest.mark.asyncio
async def test_unsupported_image_and_usage_remain_unverified(env):
    report = await run_preflight(flash_config(env), EchoTransport(fail_image=True, collect_usage=False))
    save_report(report_path(env), report)
    result = capabilities(env)
    assert report["status"] == "BLOCKED"
    assert report["issues"]["usage"] == "no_valid_usage"
    assert not result["vision"]
    assert result["text"] and result["native_tools"]
    assert env["DOVIDEO_MODEL_API_KEY"] not in json.dumps(report)


@pytest.mark.asyncio
async def test_foreign_response_model_does_not_verify_any_capability(env):
    report = await run_preflight(flash_config(env), EchoTransport(model="other-vlm"))
    assert not any(report["checks"].values())
    assert report["calls"] == 1


def test_missing_config_and_explicit_foreign_model(env):
    assert current_report({})["status"] == "BLOCKED"
    for model in ["deepseek-v4-pro", "deepseek-chat", "gpt", "other-flash"]:
        with pytest.raises(ProviderConfigurationError):
            flash_config(dict(env, DOVIDEO_MODEL_MODEL=model))
    assert not capabilities({})["text"]
