"""Exercise source versioning and readback through the upload API."""
from hashlib import sha256
import os
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from cutsieve.presentation.api.app import create_app


def sign_in(client, name, *, register=False):
    credentials = dict(username=name, password="local-test-password")
    if register:
        client.post("/user/register", json=dict(credentials, nickname=name)).raise_for_status()
    response = client.post("/user/login", json=credentials)
    response.raise_for_status()
    client.headers["Authorization"] = "Bearer " + response.json()["data"]["token"]


@pytest.mark.real_media
def test_same_name_versions_and_reopening_project(tmp_path):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required for real source version test")
    work = tmp_path / "app"
    with TestClient(create_app(work_dir=work)) as client:
        sign_in(client, "maker", register=True)
        project = client.post("/v1/projects", json={"title": "旧片重剪"}).json()["project_id"]
        uploaded_sources = []
        for color in ("red", "blue"):
            path = tmp_path / f"{color}.mp4"
            subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"color={color}:s=64x48:d=0.3",
                            "-c:v", "libx264", str(path)], check=True)
            contents = path.read_bytes()
            upload = client.post(f"/v1/projects/{project}/assets", json=dict(filename="scene.mp4",
                size_bytes=len(contents), content_type="video/mp4", sha256=sha256(contents).hexdigest()))
            upload.raise_for_status()
            upload = upload.json()
            uploaded = client.put(upload["parts"][0]["url"], content=contents)
            uploaded.raise_for_status()
            url = "/v1/uploads/" + upload["upload_id"] + "/complete"
            complete = {"idempotency_key": "version-" + color, "parts": [{"part_number": 1, "etag": uploaded.headers["ETag"]}]}
            response = client.post(url, json=complete)
            response.raise_for_status()
            asset = response.json()
            assert client.post(url, json=complete).json() == asset
            uploaded_sources.append((asset, path))
        assert uploaded_sources[0][0]["media_revision_id"] != uploaded_sources[1][0]["media_revision_id"]
        assert len(client.get(f"/v1/projects/{project}/assets").json()["items"]) == 2

    with TestClient(create_app(work_dir=work)) as client:
        sign_in(client, "maker")
        for asset, path in uploaded_sources:
            revision = client.get("/v1/media-revisions/" + asset["media_revision_id"]).json()
            artifact = client.get("/v1/artifacts/" + revision["source_artifact_id"]).json()
            assert client.get(artifact["url"]).content == path.read_bytes()
        sign_in(client, "other", register=True)
        assert client.get(f"/v1/projects/{project}/assets").status_code == 404
        assert client.get("/v1/media-revisions/" + asset["media_revision_id"]).status_code == 404


def test_upload_validates_format_and_actual_bytes(tmp_path):
    with TestClient(create_app(work_dir=tmp_path)) as client:
        sign_in(client, "editor", register=True)
        project = client.post("/v1/projects", json={"title": "上传"}).json()["project_id"]
        assets = f"/v1/projects/{project}/assets"
        body = dict(filename="scene.mp4", content_type="video/mp4", size_bytes=4)
        assert client.post(assets, json=dict(body, filename="scene.txt")).status_code == 422
        upload = client.post(assets, json=body).json()
        url = "/v1/uploads/" + upload["upload_id"]
        refresh = client.post(url + "/refresh", json=dict(part_numbers=[1], idempotency_key="try-again"))
        assert refresh.json()["upload_id"] == upload["upload_id"]
        put = refresh.json()["parts"][0]["url"]
        assert client.put(put, content=b"12345").status_code == 413
        assert client.put(put, content=b"123").status_code == 422
        etag = client.put(put, content=b"1234").headers["ETag"]
        assert client.put(put, content=b"4321").status_code == 409
        result = client.post(url + "/complete", json=dict(parts=[dict(part_number=1, etag=etag)],
                                                           idempotency_key="malformed-source"))
        # ffprobe may be absent on a developer machine.
        assert result.status_code == (422 if shutil.which("ffprobe", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
                                      or os.getenv("CUTSIEVE_FFPROBE") else 503)
        assert client.get("/v1/assets/" + upload["asset_id"]).json()["source_artifact_id"] is None
