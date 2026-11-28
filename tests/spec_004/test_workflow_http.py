"""Structured search verification stays available alongside native conversation turns."""

from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from cutsieve.presentation.api.app import create_app


@pytest.mark.real_media
def test_structured_text_and_image_workflows_keep_evidence_and_tombstone_contract(
    tmp_path, sample_video, upload_video, wait_state, monkeypatch,
):
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_LIVE_MODEL", "0")
    with TestClient(create_app(work_dir=tmp_path)) as client:
        project = client.post("/v1/projects", json={"title": "Movie"}).json()["project_id"]
        asset = upload_video(client, project, sample_video)
        asset_id, media_id = asset["asset_id"], asset["media_revision_id"]
        index = client.get(f"/v1/assets/{asset_id}/processing-runs").json()["items"][0]["processing_run_id"]
        wait_state(client, f"/v1/processing-runs/{index}", ("SUCCEEDED", "PARTIAL"))
        url = f"/v1/projects/{project}/search-workflows"
        scope = dict(project_id=project, media_revision_ids=[media_id])
        query = dict(scope=scope, top_k=5, query_mode="COUNT", text='字幕：「Where is my ticket?」',
                     range=dict(start_us=200_000, end_us=950_000))

        def submit(query, key):
            response = client.post(url, json=dict(search=query, idempotency_key=key))
            assert response.status_code == 202, response.text
            run = response.json()["run_id"]
            path = "/v1/workflow-runs/" + run
            state = wait_state(client, path)
            return state, client.get(path + "/result").json()

        state, result = submit(query, "count-quoted-dialogue")
        assert result["found_event_count"] == 1 and result["count_qualifier"] == "WITHIN_PROTOCOL"
        assert result["citations"][0]["start_us"] == 300_000
        retry = client.post(url, json=dict(search=query, idempotency_key="count-quoted-dialogue"))
        assert retry.json()["run_id"] == state["run_id"]
        different = client.post(url, json=dict(search=dict(query, query_mode="ALL"), idempotency_key="count-quoted-dialogue"))
        assert different.status_code == 409
        wrong = client.post(url, json=dict(search=dict(query, image_artifact_id=state["run_id"]),
                                         idempotency_key="wrong-image-key"))
        assert wrong.status_code == 404
        rows = client.get(f"/v1/assets/{asset_id}/coverage").json()["items"]
        key = next(row["frame_artifact_ids"][0] for row in rows if row["stage"] == "DECODED" and row["modality"] == "VISUAL")
        photo = client.get(client.get("/v1/artifacts/" + key).json()["url"]).content
        with Image.open(BytesIO(photo)) as image, BytesIO() as buffer:
            image.resize((32, 32)).save(buffer, "PNG")
            resized = buffer.getvalue()
        for index, image in enumerate((photo, resized)):
            uploaded = client.post(f"/v1/projects/{project}/query-images",
                                   files={"file": ("query.png", image, "image/png")})
            uploaded.raise_for_status()
            _, found = submit(dict(scope=scope, top_k=5, query_mode="LOCAL", image_artifact_id=uploaded.json()["artifact_id"]),
                              f"image-query-{index}")
            assert found["claims"] and all(claim["support"] == "UNCERTAIN" for claim in found["claims"])
            assert found["coverage_summary"]["verified_ranges"] == []
        result_id = state["result_artifact_id"]
        artifact = client.get("/v1/artifacts/" + result_id).json()
        assert client.get(artifact["url"]).json() == result
        monkeypatch.setenv("CUTSIEVE_WORKFLOWS_ENABLED", "false")
        assert client.post(url, json=dict(search=query, idempotency_key="disabled-search")).status_code == 503
        assert client.get(artifact["url"]).status_code == 200
        client.delete("/v1/assets/" + asset_id).raise_for_status()
        path = "/v1/workflow-runs/" + state["run_id"]
        for target in (artifact["url"], "/v1/artifacts/" + result_id, path + "/result", path + "/events"):
            assert client.get(target).status_code == 404
        assert client.get(f"/v1/projects/{project}/workflow-runs").json()["items"] == []
