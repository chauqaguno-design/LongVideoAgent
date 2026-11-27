"""Exercise the automatic route across evidence selection, visual review and clipping."""

import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest

from cutsieve.application.edit_instructions import referenced_edit, relative_window
from cutsieve.application.edit_timeline import EditError
from cutsieve.application.workflow_state import DEFAULT_BUDGET, NeedsInput
from cutsieve.infrastructure.providers.workflow_flash import FlashPlanner
from cutsieve.presentation.api.app import create_app
from test_conversation_runs import post, source, upload, wait


def test_context_limits_and_reference_survive_natural_wording():
    plan = referenced_edit("从第二个候选2秒前开始，最长不超过4秒，保留原声", candidate=True)
    assert plan == dict(command="candidate", before_us=2_000_000, max_us=4_000_000, audio="KEEP_SOURCE")
    assert relative_window(dict(start_us=4_000_000, end_us=8_000_000), 20_000_000, plan) == dict(
        start_us=2_000_000, end_us=6_000_000
    )
    with pytest.raises(NeedsInput, match="依据"):
        relative_window(dict(start_us=4_000_000, end_us=8_000_000), 20_000_000, dict(plan, max_us=1_000_000))
    assert referenced_edit("把上一版直接导出", candidate=False) == {"command": "render_current"}
    assert referenced_edit("片尾向后延伸3秒", candidate=False) == {
        "command": "adjust_clip",
        "after_us": 3_000_000,
        "last": True,
    }
    assert referenced_edit("把这个候选剪下来，前提是他真的偷了车", candidate=True) is None


def test_flash_planner_uses_configured_identity_and_protocol():
    calls = []

    class Transport:
        async def post(self, url, *, headers, json, timeout):
            calls.append((url, headers, json, timeout))
            return {"model": "local-vlm-v1"}

    env = {
        "CUTSIEVE_MODEL_BASE_URL": "http://127.0.0.1:8000/v1",
        "CUTSIEVE_MODEL_MODEL": "local-vlm",
        "CUTSIEVE_MODEL_VERSION": "local-vlm-v1",
        "CUTSIEVE_WORKFLOW_LIVE_MODEL": "1",
    }
    planner = FlashPlanner(env, Transport())
    identity = planner.identity(vision=True)
    assert identity["model"] == "local-vlm-v1" and identity["mode"] == "json"
    assert planner.identity() == identity
    assert planner.request([], identity, [], 1) == {"model": "local-vlm-v1"}
    url, headers, payload, timeout = calls[0]
    assert url.endswith("/v1/chat/completions") and headers == {} and timeout == 1
    assert payload["model"] == "local-vlm" and payload["response_format"] == {"type": "json_object"}

    env["CUTSIEVE_MODEL_NATIVE_TOOLS"] = "1"
    with pytest.raises(EditError) as changed:
        planner.request([], identity, [], 1)
    assert changed.value.code == "MODEL_CHANGED" and len(calls) == 1
    tools = [{"type": "function", "function": {"name": "read_evidence"}}]
    planner.request([], planner.identity(), tools, 1)
    assert calls[-1][2]["tools"] == tools


@pytest.mark.real_media
def test_routed_visual_question_and_edit_with_unrelated_dialogue(tmp_path, monkeypatch, source):
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_LIVE_MODEL", "0")
    with TestClient(create_app(work_dir=tmp_path / "app")) as client:
        project = post(client, "/v1/projects", title="画面与台词")["project_id"]
        mid = upload(client, project, source)

        def say(text, parent=None):
            body = dict(message=text, media_revision_id=mid, idempotency_key=str(uuid4()))
            if parent:
                body["parent_run_id"] = parent
            return post(client, f"/v1/projects/{project}/conversation-runs", **body)["run_id"]

        for text in ("帮我核实说「Ticket」时画面里是否有人跑步", "找到有人跑步的画面并剪出来"):
            run = say(text)
            state = wait(client, run, "AWAITING_INPUT")
            assert state["result_artifact_id"] and state["budget_usage"]["model_calls"] == 0
            result = client.get(f"/v1/workflow-runs/{run}/result").json()
            assert not any(c["support"] == "SUPPORTED" for c in result["claims"])
            assert "revision_id" not in result
            turn = client.get(f"/v1/workflow-runs/{run}/conversation").json()["turns"][-1]
            assert turn["revision_id"] is None
            if "Ticket" in text:
                assert result["claims"] and turn["candidates"]
            else:
                assert not turn["candidates"]  # The offline lexical index has no running scene.

        found = say("哪一段说了「Ticket」？")
        wait(client, found)
        parent = client.get(f"/v1/workflow-runs/{found}/conversation").json()["turns"][-1]
        answer = say("第二个候选有什么证据？", found)
        wait(client, answer)
        result = client.get(f"/v1/workflow-runs/{answer}/result").json()
        assert {eid for c in result["claims"] for eid in c["evidence_ids"]} == set(
            parent["candidates"][1]["evidence_ids"]
        )
        assert result["coverage_summary"]["requested_ranges"][0]["range"] == parent["candidates"][1]["range"]


class LocalVisualProtocol:
    """A controlled adapter checks the real, ordered JPEG inputs. No HTTP inference is sent."""

    env = dict(
        CUTSIEVE_CHAT_LIVE_MODEL="1",
        CUTSIEVE_WORKFLOW_LIVE_MODEL="1",
        CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS="10000",
        CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="50",
        CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="50",
        CUTSIEVE_FLASH_PRICE_VERSION="fixture",
    )

    def __init__(self):
        self.sequences = []

    def identity(self, *, vision=False):
        return dict(model="local-protocol", fingerprint="vision-fixture" if vision else "fixture", mode="json")

    def request(self, dialogs, identity, tools, deadline):
        if dialogs[0]["content"].startswith("Extract editing constraints"):
            value = dict(
                type="FINAL",
                plan=dict(
                    query="字幕：「Ticket」",
                    must_include=[],
                    must_exclude=["水印"],
                    order_policy="SOURCE_ORDER",
                    audio="KEEP_SOURCE",
                ),
            )
        else:
            content = dialogs[1]["content"]
            inputs = json.loads(content[0]["text"])
            if isinstance(inputs["goal"], dict):
                assert inputs["goal"]["must_exclude"] == ["水印"]
            times = inputs["times_us"]
            pictures = [part["image_url"]["url"] for part in content[1:]]
            assert len(times) == len(pictures) and times == sorted(set(times))
            assert all(image.startswith("data:image/jpeg;base64,") for image in pictures)
            self.sequences.append(times)
            value = dict(
                support="SUPPORTED" if len(set(pictures)) >= 2 else "UNCERTAIN",
                reason="在列明的采样时刻，测试图案画面发生变化。",
                frame_indices=[0, len(times) - 1],
            )
        return dict(
            choices=[dict(message=dict(content=json.dumps(value)))],
            usage=dict(prompt_tokens=25, completion_tokens=8),
        )


@pytest.mark.real_media
def test_bounded_inspection_supplies_visual_evidence_before_edit(tmp_path, monkeypatch, source):
    with TestClient(create_app(work_dir=tmp_path / "app")) as client:
        provider = LocalVisualProtocol()
        client.app.state.workflow_worker.planner = provider
        project = post(client, "/v1/projects", title="原片复核")["project_id"]
        mid = upload(client, project, source)
        for editing, text in [
            (False, "核实说「Ticket」时画面是否变化"),
            (True, "先核实说「Ticket」时画面有没有变化，确认后把对应片段剪出来，排除水印"),
        ]:
            provider.sequences.clear()
            key = post(
                client,
                f"/v1/projects/{project}/conversation-runs",
                message=text,
                media_revision_id=mid,
                idempotency_key=str(uuid4()),
                budget=dict(DEFAULT_BUDGET, max_cost_microunits=10000),
            )["run_id"]
            state = wait(client, key)
            result = client.get(f"/v1/workflow-runs/{key}/result").json()
            if editing:
                assert 2 <= len(provider.sequences) <= 4  # ≤3 candidates, then one output review.
                revision = client.get(
                    f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}"
                ).json()
                evidence_ids = [e for clip in revision["clips"] for e in clip["evidence_ids"]]
            else:
                assert 1 <= len(provider.sequences) <= 3
                evidence_ids = [
                    e
                    for claim in result["claims"]
                    if claim["support"] == "SUPPORTED"
                    for e in claim["evidence_ids"]
                ]
            assert evidence_ids and state["budget_usage"]["images"] >= 2
            for eid in evidence_ids:
                evidence = client.get("/v1/evidence/" + eid).json()
                assert (
                    evidence["media_revision_id"] == mid
                    and evidence["artifact_ids"]
                    and "测试图案" in evidence["claim"]
                )

        # The model fixture does not propose replace_last. The user's explicit edit still replaces it.
        parents = revision["clips"]
        followup = post(
            client,
            f"/v1/projects/{project}/conversation-runs",
            message="把最后一段换成画面发生变化的镜头",
            parent_run_id=key,
            media_revision_id=mid,
            idempotency_key=str(uuid4()),
            budget=dict(DEFAULT_BUDGET, max_cost_microunits=10000),
        )["run_id"]
        wait(client, followup)
        updated = client.get(f"/v1/workflow-runs/{followup}/result").json()
        assert updated["edit_project_id"] == result["edit_project_id"]
        changes = client.get(
            f"/v1/edit-projects/{updated['edit_project_id']}/revisions/{updated['revision_id']}"
        ).json()
        replacement = changes["clips"][len(parents) - 1]
        assert replacement["clip_id"] == parents[-1]["clip_id"]
        assert replacement["evidence_ids"] != parents[-1]["evidence_ids"]
