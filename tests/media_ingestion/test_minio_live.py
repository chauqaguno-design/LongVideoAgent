"""Real R2 auth, MinIO and MySQL; resume across machines sharing no decoder cache."""

from dataclasses import replace
from hashlib import sha256
import os
import secrets
import shutil
import struct
import subprocess
import time

from fastapi.testclient import TestClient
import pytest

from dovideo.infrastructure import R2Settings, create_r2_infrastructure
from dovideo.infrastructure.persistence.source_tables import sources
from dovideo.infrastructure.persistence.edit_cleanup import cleanup_files
from dovideo.presentation.api import create_app
from dovideo.presentation.api.r2_runtime import ProductionR2Services
from r2_render_check import check_render, create_render

pytestmark = pytest.mark.skipif(
    os.getenv("DOVIDEO_UPLOAD_LIVE") != "1", reason="requires isolated R2 services"
)


def test_import_and_resume_across_distinct_decoder_caches(tmp_path):
    exe = shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    assert exe, "Local FFmpeg required for live acceptance"
    video = tmp_path / "source.mp4"
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=96x64:r=8:d=2",
            "-c:v",
            "libx264",
            str(video),
        ],
        check=True,
    )
    small = video.read_bytes()
    chunk = 8 * 1024**2
    movie = small + struct.pack(">I4s", chunk + 8, b"free") + bytes(chunk)
    digest = sha256(movie).hexdigest()
    captions = tmp_path / "captions.srt"
    captions.write_text("1\n00:00:00,200 --> 00:00:01,000\nvisible caption\n", encoding="utf8")
    legacy_file = tmp_path / "legacy.mkv"
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-i",
            str(video),
            "-i",
            str(captions),
            "-c:v",
            "ffv1",
            "-c:s",
            "srt",
            str(legacy_file),
        ],
        check=True,
    )
    legacy_bytes = legacy_file.read_bytes()

    def replica(folder):
        settings = replace(R2Settings.from_environment(), media_workspace=tmp_path / folder)
        infra = create_r2_infrastructure(settings)
        return TestClient(create_app(services=ProductionR2Services(infra)))

    def new_account(client):
        credentials = {"username": "vm_" + secrets.token_hex(6), "password": secrets.token_urlsafe(20)}
        client.post("/user/register", json=dict(credentials, nickname="test")).raise_for_status()
        reply = client.post("/user/login", json=credentials)
        reply.raise_for_status()
        return {"Authorization": "Bearer " + reply.json()["data"]["token"]}

    def upload_part(client, target):
        n = target["part_number"]
        reply = client.put(
            target["url"], content=movie[(n - 1) * chunk : n * chunk], headers=target["headers"]
        )
        reply.raise_for_status()
        return dict(part_number=n, etag=reply.headers["ETag"])

    with replica("node-a") as client:
        owner = new_account(client)
        foreign = new_account(client)
        client.headers.update(owner)
        project = client.post("/v1/projects", json={"title": "R2 evidence"}).json()["project_id"]
        legacy = client.post("/media/init-upload", params={"filename": "legacy.mkv", "totalChunks": 1})
        legacy.raise_for_status()
        form = dict(uploadId=legacy.json()["data"]["uploadId"], chunkIndex=0, totalChunks=1)
        client.post(
            "/media/upload-chunk", data=form, files={"file": ("part", legacy_bytes)}
        ).raise_for_status()
        completed = client.post("/media/complete-upload", params={"uploadId": form["uploadId"]})
        completed.raise_for_status()
        target = f"/v1/projects/{project}/imports/{completed.json()['data']['id']}"
        assert client.post(target, headers=foreign).status_code == 404
        other_project = client.post("/v1/projects", headers=foreign, json={"title": "foreign"}).json()[
            "project_id"
        ]
        assert client.post(target.replace(project, other_project), headers=foreign).status_code == 404
        imported = client.post(target)
        imported.raise_for_status()
        imported = imported.json()
        assert client.post(target).json()["asset_id"] == imported["asset_id"]
        asset = client.get("/v1/media-revisions/" + imported["media_revision_id"]).json()
        assert asset["sha256"] == sha256(legacy_bytes).hexdigest()
        original = client.get("/v1/artifacts/" + asset["source_artifact_id"]).json()
        assert client.get(original["url"]).content == legacy_bytes
        base = "/v1/assets/" + imported["asset_id"]
        deadline = time.monotonic() + 60
        while True:
            job = client.get(base + "/processing-runs").json()["items"][0]
            if job["status"] not in ("RUNNING", "QUEUED"):
                break
            assert time.monotonic() < deadline, job
            time.sleep(0.2)
        assert job["status"] in ("SUCCEEDED", "PARTIAL"), job
        store = client.app.state.edit_sources
        row = store._one(sources, imported["workspace_id"], sources.c.id, imported["asset_id"])
        coverage = client.get(base + "/coverage").json()["items"]
        frame = next(
            r["frame_artifact_ids"][0]
            for r in coverage
            if r["stage"] == "DECODED" and r["frame_artifact_ids"]
        )
        artifact_ids = [row.probe["caption_track_id"], row.probe["playback_artifact_id"], frame]
        linked = [client.get("/v1/artifacts/" + aid).json() for aid in artifact_ids]
        upload = client.post(
            f"/v1/projects/{project}/assets",
            json=dict(filename="film.mp4", size_bytes=len(movie), content_type="video/mp4", sha256=digest),
        ).json()
        assert upload["upload_mode"] == "MULTIPART" and len(upload["parts"]) == 2
        tail = upload_part(client, upload["parts"][1])
    with replica("node-b") as client:
        client.headers.update(owner)
        for a in linked:
            assert sha256(client.get(a["url"]).content).hexdigest() == a["sha256"]
        assert "visible caption" in client.get(linked[0]["url"]).text
        target = "/v1/uploads/" + upload["upload_id"]
        assert client.get(target, headers=foreign).status_code == 404
        status = client.get(target).json()
        assert [r["part_number"] for r in status["uploaded_parts"]] == [2]
        body = {"part_numbers": [1, 2], "idempotency_key": "resume-node-b"}
        fresh = client.post(target + "/refresh", json=body).json()["parts"]
        assert upload_part(client, fresh[1]) == tail
        assert client.put(fresh[1]["url"], content=b"x" * (len(movie) - chunk)).status_code == 409
        first = upload_part(client, fresh[0])
        body = dict(parts=[first, tail], idempotency_key="r2-completion")
        complete = client.post(target + "/complete", json=body)
        complete.raise_for_status()
        ready = complete.json()
        assert (
            client.post(target + "/complete", json=body).json()["media_revision_id"]
            == ready["media_revision_id"]
        )
        assert len(client.get("/v1/assets/" + ready["asset_id"] + "/processing-runs").json()["items"]) == 1
        store = client.app.state.edit_sources
        objects = store.objects
        key = objects.source_key(ready["workspace_id"], digest)
        response = objects.client.get_object(objects.bucket, key)
        try:
            assert sha256(response.read()).hexdigest() == digest
        finally:
            response.close()
            response.release_conn()
        rendered = create_render(client, project, ready)
    with replica("node-c") as client:
        client.headers.update(owner)
        store = client.app.state.edit_sources
        assert not store.blob(ready["workspace_id"], digest).exists()
        playback = client.get("/v1/artifacts/" + ready["source_artifact_id"]).json()
        assert client.get(playback["url"]).content == movie
        assert store.blob(ready["workspace_id"], digest).is_file()
        assert (
            client.get("/v1/artifacts/" + ready["source_artifact_id"], headers=foreign).status_code == 404
        )
        check_render(client, rendered)
        parts = cleanup_files(store, older_than_seconds=0, apply=True)
        prefix = f"videomind/parts/{ready['workspace_id']}/{upload['upload_id']}/"
        assert len([key for key in parts if key.startswith(prefix)]) == 2
        assert (
            client.post(target + "/complete", json=body).json()["media_revision_id"]
            == ready["media_revision_id"]
        )
