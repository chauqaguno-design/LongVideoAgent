"""Real HTTP + FFmpeg round trip through a user request, checkpoint, preview and replay."""

import hashlib
from io import BytesIO
import os
import shutil
import subprocess
import time

from fastapi.testclient import TestClient
import pytest
from PIL import Image

from cutsieve.presentation.api.app import create_app


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
    assert exact["claims"] and all(c["support"] == "UNCERTAIN" for c in exact["claims"])
    proofs = [client.get("/v1/evidence/" + eid).json() for c in exact["claims"] for eid in c["evidence_ids"]]
    assert any(key in proof["artifact_ids"] for proof in proofs)
    with Image.open(BytesIO(picture)) as image, BytesIO() as out:
        image.resize((32, 32)).save(out, "PNG")
        approximate = ask(out.getvalue(), "resized-query-image")
    assert approximate["claims"] and all(c["support"] == "UNCERTAIN" for c in approximate["claims"])
    assert approximate["coverage_summary"]["verified_ranges"] == []


@pytest.mark.real_media
def test_natural_time_request_preview_and_restore_after_restart(tmp_path, monkeypatch):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
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
        return c.get(url + "/result").json(), state

    work = tmp_path / "app"
    with TestClient(create_app(work_dir=work)) as c:
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
        monkeypatch.setenv("CUTSIEVE_WORKFLOWS_ENABLED", "false")
        assert c.post("/v1/workflow-runs", json=body).status_code == 503
        assert c.get(f"/v1/workflow-runs/{state['run_id']}/result").json() == result
    monkeypatch.setenv("CUTSIEVE_WORKFLOWS_ENABLED", "true")
    with TestClient(create_app(work_dir=work)) as c:
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
