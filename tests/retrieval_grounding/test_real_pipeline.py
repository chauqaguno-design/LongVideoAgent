"""Uploaded subtitles and an actual sampled frame pass through the public v1 search routes."""

import hashlib
import os
import shutil
import subprocess
import time
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from cutsieve.presentation.api.app import create_app


@pytest.mark.real_media
def test_uploaded_source_query_image_grounded_quote_and_delete(tmp_path, monkeypatch):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("requires FFmpeg")
    for k in (
        "CUTSIEVE_SEARCH_ENCODER_URL",
        "CUTSIEVE_WHISPER_DIR",
        "CUTSIEVE_PADDLE_OCR_DIR",
    ):
        monkeypatch.delenv(k, raising=False)
    text = tmp_path / "dialogue.srt"
    text.write_text(
        "1\n00:00:00,300 --> 00:00:00,800\n给我三秒钟\n\n2\n00:00:01,500 --> 00:00:02,000\n给我三秒钟\n",
        encoding="utf8",
    )
    video = tmp_path / "scene.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x64:r=12:d=3",
            "-i",
            str(text),
            "-c:v",
            "libx264",
            "-c:s",
            "mov_text",
            str(video),
        ],
        check=True,
    )
    data = video.read_bytes()
    with TestClient(create_app(work_dir=tmp_path / "store")) as client:
        login = dict(username="researcher", password="local-demo-password")
        client.post("/user/register", json=dict(login, nickname="测试")).raise_for_status()
        token = client.post("/user/login", json=login).json()["data"]["token"]
        client.headers["Authorization"] = "Bearer " + token
        pid = client.post("/v1/projects", json={"title": "scene"}).json()["project_id"]
        ticket = client.post(
            f"/v1/projects/{pid}/assets",
            json=dict(filename=video.name, content_type="video/mp4", size_bytes=len(data)),
        ).json()
        client.put(ticket["parts"][0]["url"], content=data).raise_for_status()
        response = client.post(
            "/v1/uploads/" + ticket["upload_id"] + "/complete",
            json=dict(
                idempotency_key="complete-scene",
                parts=[dict(part_number=1, etag=hashlib.sha256(data).hexdigest())],
            ),
        )
        response.raise_for_status()
        asset = response.json()
        aid = asset["asset_id"]
        run_url = f"/v1/assets/{aid}/processing-runs"
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            runs = client.get(run_url).json()["items"]
            if runs and runs[0]["status"] in {"FAILED", "SUCCEEDED", "PARTIAL"}:
                break
            time.sleep(0.2)
        assert runs[0]["status"] in {"SUCCEEDED", "PARTIAL"}, runs
        scope = dict(project_id=pid, media_revision_ids=[asset["media_revision_id"]])
        question = dict(scope=scope, text="字幕：「给我三秒钟」", query_mode="COUNT", top_k=1)
        candidates = client.post("/v1/search", json=question).json()
        assert candidates["verification_required"] and not candidates["hits"][0]["verified"]
        service = client.app.state.evidence_search
        ws = asset["workspace_id"]
        answer = service.answer(ws, str(uuid4()), question)
        assert answer["found_event_count"] == 2 and answer["count_qualifier"] == "WITHIN_PROTOCOL", answer
        full = client.get("/v1/artifacts/" + answer["complete_event_list_artifact_id"]).json()
        assert len(client.get(full["url"]).json()["events"]) == 2
        coverage = client.get(f"/v1/assets/{aid}/coverage").json()["items"]
        fid = next(
            r["frame_artifact_ids"][0]
            for r in coverage
            if r["stage"] == "DECODED" and r["modality"] == "VISUAL"
        )
        frame = client.get("/v1/artifacts/" + fid).json()
        pixels = client.get(frame["url"]).content
        image = client.post(
            f"/v1/projects/{pid}/query-images",
            files={"file": ("frame.jpeg", pixels, "image/jpeg")},
        ).json()
        by_image = dict(
            scope=scope,
            query_mode="LOCAL",
            image_artifact_id=image["artifact_id"],
            top_k=5,
        )
        hit = client.post("/v1/search", json=by_image).json()["hits"][0]
        assert hit["range"]["end_us"] - hit["range"]["start_us"] >= 1_000_000
        pic = service.answer(ws, str(uuid4()), by_image)
        assert pic["claims"] and all(claim["support"] == "UNCERTAIN" for claim in pic["claims"])
        proof = client.get("/v1/evidence/" + hit["evidence_ids"][0])
        proof.raise_for_status()
        assert proof.json()["artifact_ids"] == [fid]
        assert proof.json()["support"] == "NOT_CHECKED"
        client.delete(f"/v1/assets/{aid}").raise_for_status()
        assert client.get("/v1/evidence/" + proof.json()["evidence_id"]).status_code == 404
        assert client.get(full["url"]).status_code == 404
