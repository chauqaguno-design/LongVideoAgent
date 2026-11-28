from base64 import b64decode
from io import BytesIO
import json
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cutsieve.application.agent_harness import args_preview, prepare_history
from cutsieve.application.workflow_checkpoint import Checkpoint
from cutsieve.presentation.api.app import create_app
from test_agent_harness import body, create_project, last_tool, start_chat


def test_compaction_keeps_complete_multi_tool_turn_and_query_image():
    cp = Checkpoint.model_validate(dict(
        spec=dict(scope=dict(project_id="p", media_revision_ids=[]), message="Find this frame"),
        tool_steps=[dict(tool_call_id="old", name="inspect_frames", summary="frame at 12.5s")],
    ))
    assert cp.tool_steps[0].args_preview == "" and cp.trace_events == []
    assert cp.usage.images_unloaded == cp.usage.compactions == 0
    image = dict(type="image_url", image_url=dict(url="data:image/jpeg;base64,AAAA"))
    user = dict(role="user", content=[dict(type="text", text=cp.spec.message), image])
    old_tool = dict(role="tool", tool_call_id="old", content=[dict(type="text", text="12.5s"), image])
    helper = [dict(role="assistant", tool_calls=[dict(id=str(i)) for i in range(7)])]
    helper += [dict(role="tool", tool_call_id=str(i), content=[image]) for i in range(7)]
    cp.messages = [
        dict(role="system", content="System"), user,
        dict(role="assistant", tool_calls=[dict(id="old")]), old_tool,
        dict(role="assistant", content="Prior context " * 100), *helper,
    ]
    cp.revision_id = "revision-7"
    cp.usage.model_calls = 4
    prepare_history(cp, {"CUTSIEVE_COMPACT_MAX_MESSAGES": "100", "CUTSIEVE_COMPACT_CHAR_LIMIT": "1200"})
    assert cp.messages[1] == user and cp.messages[-len(helper):] == helper
    assert "revision-7" in cp.messages[2]["content"] and "12.5s" in cp.messages[2]["content"]
    assert cp.usage.images_unloaded == cp.usage.compactions == 1
    assert [(e.kind, e.step) for e in cp.trace_events] == [("IMAGE_UNLOAD", 4), ("COMPACTION", 4)]
    assert Checkpoint.model_validate(cp.model_dump()) == cp


def test_preview_redacts_inline_data_and_invalid_arguments():
    assert args_preview("bad secret") == "[invalid JSON]"
    preview = args_preview(json.dumps(dict(api_key="secret-value", img="data:image/png;base64,1234")))
    assert "secret-value" not in preview and "1234" not in preview
    clipped = args_preview(json.dumps(dict(a="a" * 200, b="b" * 200)))
    assert len(clipped) <= 160 and json.loads(clipped)["preview"].endswith("…")


@pytest.mark.real_media
def test_harness_receipts_contact_sheet_unloading_and_repair(
    tmp_path, monkeypatch, fake_agent, sample_video, upload_video, wait_state,
):
    monkeypatch.setenv("CUTSIEVE_HARNESS_MAX_IMAGES", "1")
    monkeypatch.setenv("CUTSIEVE_COMPACT_MAX_MESSAGES", "9")
    tool, reply = fake_agent.tool, fake_agent.reply
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)

        def repair(request):
            invalid = last_tool(request, "invalid")
            assert invalid["code"] == "ASSET_NOT_FOUND"
            assert invalid["details"]["valid_assets"] == [
                dict(asset_id=asset["asset_id"], media_revision_id=asset["media_revision_id"],
                     filename=sample_video.name, duration_s=2),
            ]
            return reply(tool("edit", "edit_timeline", dict(audio_fade_ms=300, clips=[
                dict(start_s=0, end_s=.5),
                dict(asset_id=asset["media_revision_id"], start_s=1, end_s=1.5, audio_policy="mute"),
            ])))

        def conflict(request):
            receipt = last_tool(request, "edit")
            assert receipt["revision_no"] == 1 and receipt["duration_s"] == 1 and receipt["clip_count"] == 2
            assert [c["mute"] for c in receipt["clips"]] == [False, True]
            assert all(c["asset_id"] == asset["asset_id"] for c in receipt["clips"])
            assert receipt["audio_fade_ms"] == 300
            return reply(tool("conflict", "edit_timeline", dict(expected_revision=0, audio_fade_ms=500)))

        def inspect(request):
            error = last_tool(request, "conflict")
            assert error["code"] == "REVISION_CONFLICT"
            assert error["details"]["head_revision_no"] == 1 and len(error["details"]["current_clips"]) == 2
            return reply(tool("sheet", "inspect_frames", dict(timestamps_s=[.2, .7, 1.4], contact_sheet=True)))

        def verify(request):
            result = last_tool(request, "sheet")
            assert result["contact_sheet"] and [f["timestamp_us"] for f in result["frames"]] == [200000, 700000, 1400000]
            text, image = request["messages"][-1]["content"]
            assert text["type"] == "text"
            with Image.open(BytesIO(b64decode(image["image_url"]["url"].split(",")[1]))) as photo:
                assert photo.size == (768, 432) and photo.format == "JPEG"
            return reply(tool("overview", "get_media_overview"))

        def final(request):
            sheet = next(msg for msg in request["messages"] if msg.get("tool_call_id") == "sheet")
            assert all(part["type"] == "text" for part in sheet["content"])
            assert "已卸载历史视觉帧" in sheet["content"][-1]["text"]
            assert json.loads(sheet["content"][0]["text"])["frames"]
            return reply(text="Verified the timeline.")

        fake_agent.responses = [
            reply(tool("invalid", "edit_timeline", dict(clips=[dict(asset_id=str(uuid4()), start_s=0, end_s=.5)]))),
            repair, conflict, inspect, verify, final,
        ]
        endpoint, _ = start_chat(client, project, "Verify the timeline.")
        wait_state(client, endpoint)
        harness = body(client.get(endpoint + "/harness"))
        assert harness["harness_version"] == 2 and harness["status"] == "SUCCEEDED"
        assert harness["usage"]["images_unloaded"] == 1 and harness["usage"]["compactions"] >= 1
        assert set(e["kind"] for e in harness["trace_events"]) == {"IMAGE_UNLOAD", "COMPACTION", "MODEL_TURN", "SELF_REPAIR"}
        assert sum(e["input_tokens"] for e in harness["trace_events"]) == 600
        assert sum(e["output_tokens"] for e in harness["trace_events"]) == 120
        assert all(e["duration_ms"] >= 0 for e in harness["trace_events"] if e["kind"] == "MODEL_TURN")
        assert [s["step_index"] for s in harness["tool_steps"]] == list(range(1, 6))
        assert all(s["duration_ms"] >= 0 and s["args_preview"] for s in harness["tool_steps"])
        assert harness["tool_steps"][0]["error_code"] == "ASSET_NOT_FOUND"
        assert harness["tool_steps"][1]["repaired"] and harness["tool_steps"][3]["images_added"] == 1
        assert body(client.get(endpoint + "/harness?offset=3"))["tool_steps"] == harness["tool_steps"][3:]
        result = body(client.get(endpoint + "/result"))
        path = f"/v1/edit-projects/{result['edit_project_id']}/revisions"
        assert [r["audio_fade_ms"] for r in body(client.get(path))["items"]] == [300, 0]
