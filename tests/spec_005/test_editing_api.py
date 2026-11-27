"""Authenticated upload, revision-bound export and artifact playback over HTTP."""
import hashlib
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from dovideo.presentation.api.app import create_app


@pytest.mark.real_media
def test_edit_upload_and_render_job(tmp_path):
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("Real editing API integration needs FFmpeg")
    video = tmp_path / "source.mp4"
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=25:duration=2",
                    "-c:v", "libx264", str(video)], check=True)
    data = video.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    with TestClient(create_app(work_dir=tmp_path / "work")) as client:
        def login(name):
            body = dict(username=name,password="demo-password")
            assert client.post("/user/register", json=dict(body, nickname=name)).status_code == 200
            return {"Authorization": "Bearer " + client.post("/user/login", json=body).json()["data"]["token"]}
        alice, bob = login("editor"), login("viewer")
        def post(path, body, expected):
            reply = client.post(path, json=body, headers=alice)
            assert reply.status_code == expected, reply.text
            return reply.json()
        project = post("/v1/projects", dict(title="Demo"), 201)
        project_id = project["project_id"]
        upload = post(f"/v1/projects/{project_id}/assets",
                      dict(filename="source.mp4", size_bytes=len(data), content_type="video/mp4"), 201)
        assert client.put(upload["parts"][0]["url"], content=data).status_code == 200
        media = post(f"/v1/uploads/{upload['upload_id']}/complete", dict(idempotency_key="complete-demo",
                     parts=[dict(part_number=1,etag=digest)]), 202)
        profile = dict(width=96, height=64, fps_num=25, fps_den=1, audio_sample_rate=48000)
        edit = post("/v1/edit-projects", dict(project_id=project_id, title="Demo", output_profile=profile,
                    idempotency_key="create-demo"), 201)
        assert client.get(f"/v1/edit-projects/{edit['edit_project_id']}", headers=bob).status_code == 404
        clip = dict(media_revision_id=media["media_revision_id"], evidence_ids=[], audio_policy="keep")
        ops = [dict(op="append_clip",clip=dict(clip,source_start_us=start,source_end_us=end))
               for start,end in [(0,360000),(1000000,1440000)]]
        op_url = f"/v1/edit-projects/{edit['edit_project_id']}/operations"
        body = dict(expected_revision=0, reason="two moments", operations=ops, idempotency_key="patch-demo")
        receipt = post(op_url, body, 201)
        assert post(op_url, body, 200)["replayed"]
        broken = dict(body, operations=[dict(op="append_clip", clip=dict(clip, start_ms=0, end_ms=100))])
        assert client.post(op_url, json=broken, headers=alice).status_code == 422
        job = post("/v1/render-jobs", dict(revision_id=receipt["revision_id"],profile="PREVIEW",
                   idempotency_key="render-demo"), 202)
        for _ in range(150):
            job = client.get("/v1/render-jobs/"+job["render_job_id"], headers=alice).json()
            if job["status"] in {"SUCCEEDED","FAILED"}:
                break
            time.sleep(.2)
        assert job["status"] == "SUCCEEDED", job
        artifact_url = "/v1/artifacts/"+job["artifact_id"]
        assert client.get(artifact_url,headers=bob).status_code == 404
        artifact = client.get(artifact_url,headers=alice).json()
        result = client.get(artifact["url"])
        assert result.status_code == 200
        assert hashlib.sha256(result.content).hexdigest() == artifact["sha256"]
        assert client.get(artifact["url"]+"x").status_code == 404
        ref = client.get("/v1/artifacts/"+job["compiled_manifest_artifact_id"], headers=alice).json()
        manifest = client.get(ref["url"]).json()
        assert manifest["total_duration_us"] == 800000

        base = dict(revision_id=receipt["revision_id"], profile="EXPORT", idempotency_key="export-demo")
        legacy = post("/v1/render-jobs", base, 202)
        assert post("/v1/render-jobs?format=mp4", base, 202)["render_job_id"] == legacy["render_job_id"]
        post("/v1/render-jobs?format=webm", base, 409)
        post("/v1/render-jobs?format=avi", base, 422)
        post("/v1/render-jobs?format=mkv", dict(base, profile="PREVIEW"), 422)
        slots = {legacy["cache_key"]}
        for fmt, mime in [("webm", "video/webm"), ("mkv", "video/x-matroska")]:
            request = dict(base, idempotency_key="export-" + fmt)
            path = "/v1/render-jobs?format=" + fmt
            job = post(path, request, 202)
            assert post(path, dict(request, idempotency_key="replay-" + fmt), 202)["render_job_id"] == job["render_job_id"]
            assert job["cache_key"] not in slots
            slots.add(job["cache_key"])
            for _ in range(150):
                job = client.get("/v1/render-jobs/" + job["render_job_id"], headers=alice).json()
                if job["status"] in {"SUCCEEDED", "FAILED"}:
                    break
                time.sleep(.2)
            assert job["status"] == "SUCCEEDED", job
            artifact = client.get("/v1/artifacts/" + job["artifact_id"], headers=alice).json()
            download = client.get(artifact["url"])
            assert artifact["mime_type"] == download.headers["content-type"] == mime
            assert download.content.startswith(bytes.fromhex("1a45dfa3"))
            assert hashlib.sha256(download.content).hexdigest() == artifact["sha256"]
