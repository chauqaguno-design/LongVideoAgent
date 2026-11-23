"""Real HTTP + FFmpeg round trip through a user request, checkpoint, preview and replay."""

import hashlib
import json
from io import BytesIO
import os
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
import pytest
from PIL import Image

from dovideo.application.spec_contracts import validate_contract
from dovideo.application.workflow_state import DEFAULT_BUDGET
from dovideo.presentation.api.app import create_app


class TeamFixture:
    """Records four independent member conversations; attempts one unauthorized write as Editor."""

    env = dict(
        DOVIDEO_WORKFLOW_LIVE_MODEL="1",
        DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="10000",
        DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="50",
        DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="50",
        DOVIDEO_FLASH_PRICE_VERSION="local-fixture",
    )
    roles = []

    def identity(self, *, vision=False):
        return dict(model="same-test-deployment", mode="native", fingerprint="same-fingerprint")

    def request(self, dialogs, identity, tools, deadline):
        role = dialogs[0]["content"].split(" in a film")[0].split()[-1]
        assert identity == self.identity()
        self.roles.append(role)
        inputs = json.loads(dialogs[1]["content"])
        if role != "COORDINATOR":
            assert inputs["previous_handoff"]["artifact_id"]
        if role == "EDITOR" and len(dialogs) == 2:
            assert "edit__apply_patch" not in [x["function"]["name"] for x in tools]
            msg = dict(
                role="assistant",
                tool_calls=[
                    dict(
                        id="try-write",
                        type="function",
                        function=dict(name="edit__apply_patch", arguments="{}"),
                    )
                ],
            )
        else:
            if role == "EDITOR":
                assert dialogs[-1]["tool_call_id"] == "try-write" and json.loads(dialogs[-1]["content"]) == {
                    "error": "TOOL_DENIED"
                }
            if role == "MATERIAL":
                assert inputs["evidence_ids"] and inputs["intervals"]
            if role == "REVIEWER":
                assert (
                    inputs["revision_id"]
                    and inputs["render_job_id"]
                    and inputs["checks"]["media"] == "PASSED"
                )
            plan = dict(
                decision="APPROVE",
                note="核对本阶段输入",
                evidence_ids=inputs["evidence_ids"],
                candidate_order=[],
            )
            msg = dict(role="assistant", content=json.dumps(dict(type="FINAL", plan=plan)))
        return dict(choices=[dict(message=msg)], usage=dict(prompt_tokens=60, completion_tokens=10))


def check_image_workflow(client, project, media, post, wait):
    rows = client.get(f"/v1/assets/{media['asset_id']}/coverage").json()["items"]
    key = next(
        r["frame_artifact_ids"][0] for r in rows if r["stage"] == "DECODED" and r["modality"] == "VISUAL"
    )
    picture = client.get(client.get("/v1/artifacts/" + key).json()["url"]).content

    def ask(data, idempotency_key):
        uploaded = client.post(
            f"/v1/projects/{project}/query-images", files={"file": ("query.png", data, "image/png")}
        )
        uploaded.raise_for_status()
        query = dict(
            scope=dict(project_id=project, media_revision_ids=[media["media_revision_id"]]),
            image_artifact_id=uploaded.json()["artifact_id"],
            top_k=5,
            query_mode="LOCAL",
        )
        run = post(
            client,
            f"/v1/projects/{project}/search-workflows",
            dict(search=query, idempotency_key=idempotency_key),
        )
        return wait(client, run["run_id"])[0]

    exact = ask(picture, "same-pixel-image")
    assert exact["coverage_summary"]["protocol_id"] == "exact-rgb-v1"
    eid = next(c["evidence_ids"][0] for c in exact["claims"] if c["support"] == "SUPPORTED")
    assert key in client.get("/v1/evidence/" + eid).json()["artifact_ids"]
    with Image.open(BytesIO(picture)) as image, BytesIO() as out:
        image.resize((32, 32)).save(out, "PNG")
        approximate = ask(out.getvalue(), "resized-query-image")
    assert approximate["coverage_summary"]["protocol_id"] != "exact-rgb-v1"


@pytest.mark.real_media
def test_natural_time_request_preview_and_restore_after_restart(tmp_path, monkeypatch):
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    source = tmp_path / "test.mp4"
    subs = tmp_path / "captions.srt"
    subs.write_text("1\n00:00:01,200 --> 00:00:02,400\nWhere is my ticket?\n", encoding="utf8")
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=160x90:r=25:d=4",
            "-i",
            str(subs),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-c:v",
            "libx264",
            "-c:s",
            "mov_text",
            str(source),
        ],
        check=True,
    )
    media = source.read_bytes()
    creds = dict(username="workflow", password="test-password")

    def sign_in(c):
        c.headers["Authorization"] = "Bearer " + c.post("/user/login", json=creds).json()["data"]["token"]

    def post(c, path, body):
        resp = c.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    def wait(c, key):
        url = "/v1/workflow-runs/" + key
        for _ in range(180):
            state = c.get(url).json()
            if state["status"] in {"FAILED", "SUCCEEDED", "AWAITING_INPUT"}:
                break
            time.sleep(0.1)
        assert state["status"] == "SUCCEEDED", state
        validate_contract("WorkflowRun", state)
        return c.get(url + "/result").json(), state

    work = tmp_path / "app"
    monkeypatch.setenv("DOVIDEO_WORKFLOW_TEAM", "true")
    with TestClient(create_app(work_dir=work)) as c:
        post(c, "/user/register", dict(creds, nickname="editor"))
        sign_in(c)
        pid = post(c, "/v1/projects", {"title": "Movie"})["project_id"]
        upl = post(
            c,
            f"/v1/projects/{pid}/assets",
            dict(filename="scene.mp4", size_bytes=len(media), content_type="video/mp4"),
        )
        put = c.put(upl["parts"][0]["url"], content=media)
        asset = post(
            c,
            f"/v1/uploads/{upl['upload_id']}/complete",
            dict(parts=[dict(part_number=1, etag=put.headers["ETag"])], idempotency_key="complete-asset"),
        )
        body = dict(
            scope=dict(project_id=pid, media_revision_ids=[asset["media_revision_id"]]),
            intent="EDIT",
            message="剪出 0 到 0.4 秒和 1 到 2 秒并导出",
            idempotency_key="first-request",
        )
        workflow = post(c, "/v1/workflow-runs", body)
        result, state = wait(c, workflow["run_id"])
        assert state["budget_usage"]["model_calls"] == 0
        assert result["brief"]["selection_mode"] == "EXPLICIT_RANGES"
        validate_contract("EditBrief", result["brief"])
        metadata = c.get("/v1/artifacts/" + result["preview_artifact_id"]).json()
        out = c.get(metadata["url"]).content
        assert out[4:8] == b"ftyp" and hashlib.sha256(out).hexdigest() == metadata["sha256"]
        export = c.get("/v1/artifacts/" + result["export_artifact_id"]).json()
        assert export["kind"] == "EXPORT" and export["timeline_revision_id"] == result["revision_id"]
        assert c.get(export["url"]).content[4:8] == b"ftyp"
        assert c.get(f"/v1/workflow-runs/{state['run_id']}/jobs/{result['export_job_id']}").json()["terminal"]
        result_meta = c.get("/v1/artifacts/" + state["result_artifact_id"]).json()
        assert c.get(result_meta["url"]).json() == result
        revision = c.get(
            f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}"
        ).json()
        assert revision["total_duration_us"] == 1_400_000 and len(revision["clips"]) == 2
        events = c.get(
            f"/v1/workflow-runs/{state['run_id']}/events",
            headers={"Last-Event-ID": workflow["last_event_id"]},
        )
        assert events.status_code == 200 and "event: completed" in events.text
        tasks = c.get(f"/v1/workflow-runs/{state['run_id']}/team-tasks").json()["items"]
        assert len(tasks) == 6 and all(task["status"] == "SUCCEEDED" for task in tasks)
        assert {task["role"] for task in tasks} == {"COORDINATOR", "MATERIAL", "EDITOR", "REVIEWER"}
        controlled = TeamFixture()
        controlled.roles = []
        c.app.state.workflow_worker.planner = controlled
        group = post(
            c,
            "/v1/workflow-runs",
            dict(
                body,
                message="剪出台词「Where is my ticket?」",
                idempotency_key="four-role-edit",
                budget=dict(DEFAULT_BUDGET, max_cost_microunits=10000),
            ),
        )
        team_result, team_state = wait(c, group["run_id"])
        assert controlled.roles == ["COORDINATOR", "MATERIAL", "EDITOR", "EDITOR", "REVIEWER"]
        assert (
            team_result["team_review"]["model"] == "same-test-deployment"
            and team_state["budget_usage"]["model_calls"] == 5
        )
        monkeypatch.setenv("DOVIDEO_WORKFLOWS_ENABLED", "false")
        assert c.post("/v1/workflow-runs", json=body).status_code == 503
        assert c.get(f"/v1/workflow-runs/{state['run_id']}/result").json() == result
    monkeypatch.setenv("DOVIDEO_WORKFLOWS_ENABLED", "true")
    monkeypatch.setenv("DOVIDEO_WORKFLOW_TEAM", "false")
    with TestClient(create_app(work_dir=work)) as c:
        sign_in(c)
        assert c.get(f"/v1/workflow-runs/{state['run_id']}/result").json() == result
        change = post(
            c,
            "/v1/workflow-runs",
            dict(
                body,
                message="去掉最后一段",
                edit_project_id=result["edit_project_id"],
                idempotency_key="followup-request",
            ),
        )
        revised, _ = wait(c, change["run_id"])
        assert revised["revision_id"] != result["revision_id"]
        newrev = c.get(
            f"/v1/edit-projects/{result['edit_project_id']}/revisions/{revised['revision_id']}"
        ).json()
        assert newrev["total_duration_us"] == 400_000

        def followup(message, key):
            request = dict(
                body, message=message, edit_project_id=result["edit_project_id"], idempotency_key=key
            )
            accepted = post(c, "/v1/workflow-runs", request)
            edited, _ = wait(c, accepted["run_id"])
            return c.get(
                f"/v1/edit-projects/{edited['edit_project_id']}/revisions/{edited['revision_id']}"
            ).json()

        replaced = followup("把最后一段换成剪出 2 到 3 秒", "replace-last-clip")
        assert [(v["source_start_us"], v["source_end_us"]) for v in replaced["clips"]] == [
            (2_000_000, 3_000_000)
        ]
        restored = followup("恢复版本 1", "restore-first-edit")
        assert restored["clips"] == revision["clips"] and restored["revision_no"] > replaced["revision_no"]
        quoted = post(
            c,
            "/v1/workflow-runs",
            dict(body, message="剪出台词「Where is the bus?」", idempotency_key="cut-dialogue"),
        )
        quote_url = "/v1/workflow-runs/" + quoted["run_id"]
        for _ in range(100):
            waiting = c.get(quote_url).json()
            if waiting["status"] == "AWAITING_INPUT":
                break
            time.sleep(0.1)
        assert waiting["status"] == "AWAITING_INPUT" and waiting["awaiting_input"]["kind"] == "CLARIFICATION"
        post(
            c,
            quote_url + "/resume",
            dict(
                expected_state_version=waiting["state_version"],
                idempotency_key="correct-dialogue",
                message="剪出台词「Where is my ticket?」并保留原声",
            ),
        )
        found, run = wait(c, quoted["run_id"])
        assert "Where is my ticket?" in found["brief"]["goal"]
        picked = c.get(
            f"/v1/edit-projects/{found['edit_project_id']}/revisions/{found['revision_id']}"
        ).json()
        assert [(v["source_start_us"], v["source_end_us"]) for v in picked["clips"]] == [
            (1_200_000, 2_400_000)
        ]
        assert picked["clips"][0]["evidence_ids"] == run["evidence_ids"]
        assert c.get("/v1/evidence/" + run["evidence_ids"][0]).json()["claim"] == "Where is my ticket?"
        search_url = f"/v1/projects/{pid}/search-workflows"
        query = dict(
            scope=body["scope"],
            top_k=1,
            query_mode="COUNT",
            text="字幕：「Where is my ticket?」",
            range=dict(start_us=1_000_000, end_us=2_500_000),
        )
        query_body = dict(search=query, idempotency_key="count-quoted-dialogue")
        lookup = post(c, search_url, query_body)
        answer, _ = wait(c, lookup["run_id"])
        validate_contract("AnswerResult", answer)
        assert answer["found_event_count"] == 1 and answer["count_qualifier"] == "WITHIN_PROTOCOL"
        assert post(c, search_url, query_body)["run_id"] == lookup["run_id"]
        assert (
            c.post(search_url, json=dict(query_body, search=dict(query, query_mode="ALL"))).status_code == 409
        )
        assert (
            c.post(
                search_url, json=dict(query_body, search=dict(query, image_artifact_id=run["run_id"]))
            ).status_code
            == 404
        )
        public = c.get("/v1/artifacts/" + run["result_artifact_id"]).json()
        check_image_workflow(c, pid, asset, post, wait)
        assert c.get(public["url"]).status_code == 200
        c.delete("/v1/assets/" + asset["asset_id"]).raise_for_status()
        assert c.get(public["url"]).status_code == 404
        assert c.get("/v1/artifacts/" + run["result_artifact_id"]).status_code == 404
        assert c.get("/v1/workflow-runs/" + run["run_id"] + "/result").status_code == 404
        assert c.get("/v1/workflow-runs/" + run["run_id"] + "/events").status_code == 404
        assert c.get("/v1/projects/" + pid + "/workflow-runs").json()["items"] == []
