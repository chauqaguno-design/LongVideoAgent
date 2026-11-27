"""Resume an interrupted multipart upload and replay completion against the real API."""

import hashlib
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from cutsieve.infrastructure import upload_parts
from cutsieve.presentation.api.app import create_app


@pytest.mark.real_media
def test_resume_after_restart(tmp_path, monkeypatch):
    ffmpeg = shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    monkeypatch.setattr(upload_parts, "part_size", lambda total: 1024)
    src = tmp_path / "source.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x48:d=0.5", "-c:v", "libx264", str(src)],
        check=True,
    )
    data = src.read_bytes()
    credentials = dict(username="local_editor", password="only-for-this-test")

    def login(client):
        token = client.post("/user/login", json=credentials).json()["data"]["token"]
        client.headers["Authorization"] = "Bearer " + token

    def put(client, part):
        n = part["part_number"]
        block = data[(n - 1) * 1024 : n * 1024]
        reply = client.put(part["url"], content=block, headers=part["headers"])
        reply.raise_for_status()
        return dict(part_number=n, etag=reply.headers["ETag"])

    with TestClient(create_app(work_dir=tmp_path / "store")) as client:
        client.post("/user/register", json=dict(credentials, nickname="编辑")).raise_for_status()
        login(client)
        p = client.post("/v1/projects", json={"title": "分片"}).json()["project_id"]
        upload = client.post(
            f"/v1/projects/{p}/assets",
            json=dict(
                filename="film.mp4",
                size_bytes=len(data),
                content_type="video/mp4",
                sha256=hashlib.sha256(data).hexdigest(),
            ),
        ).json()
        assert upload["upload_mode"] == "MULTIPART"
        first, *rest = upload["parts"]
        receipt = put(client, first)
        assert put(client, first) == receipt
        assert client.put(first["url"], content=b"x" * 1024).status_code == 409
    with TestClient(create_app(work_dir=tmp_path / "store")) as client:
        login(client)
        path = "/v1/uploads/" + upload["upload_id"]
        assert len(client.get(path).json()["uploaded_parts"]) == 1
        body = dict(idempotency_key="complete-once", parts=[receipt])
        assert client.post(path + "/complete", json=body).status_code == 422
        fresh = client.post(
            path + "/refresh",
            json=dict(part_numbers=[p["part_number"] for p in rest], idempotency_key="resume-after-restart"),
        ).json()
        body["parts"] += [put(client, part) for part in fresh["parts"]]
        asset = client.post(path + "/complete", json=body)
        asset.raise_for_status()
        assert (
            client.post(path + "/complete", json=body).json()["media_revision_id"]
            == asset.json()["media_revision_id"]
        )
        assert client.post(path + "/complete", json=dict(body, sha256="0" * 64)).status_code == 409
        file = client.get("/v1/artifacts/" + asset.json()["source_artifact_id"]).json()
        assert client.get(file["url"]).content == data
