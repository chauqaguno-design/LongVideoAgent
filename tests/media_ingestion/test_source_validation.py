"""A valid MP4 header cannot publish unreadable source bytes as available."""

import os
import shutil
import struct
import subprocess

from fastapi.testclient import TestClient
import pytest

from dovideo.presentation.api import create_app


@pytest.mark.real_media
def test_corrupt_video_payload_with_intact_header_is_rejected(tmp_path):
    ffmpeg = shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("Local FFmpeg required")
    path = tmp_path / "damaged.mp4"
    subprocess.run(
        [ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=s=64x48:r=8:d=1", "-c:v", "libx264", str(path)],
        check=True,
    )
    data = bytearray(path.read_bytes())
    i = data.index(b"mdat")
    size = struct.unpack(">I", data[i - 4 : i])[0]
    data[i + 4 : i + size - 4] = bytes(size - 8)
    with TestClient(create_app(work_dir=tmp_path / "server")) as client:
        login = dict(username="asset_reader", password="testing-source")
        client.post("/user/register", json=dict(login, nickname="test")).raise_for_status()
        token = client.post("/user/login", json=login).json()["data"]["token"]
        client.headers["Authorization"] = "Bearer " + token
        project = client.post("/v1/projects", json={"title": "source validation"}).json()["project_id"]
        target = f"/v1/projects/{project}/assets"
        body = dict(filename="film.mp4", size_bytes=len(data), content_type="video/mp4")
        assert client.post(target, json=dict(body, filename="../film.mp4")).status_code == 422
        session = client.post(target, json=body).json()
        part = client.put(session["parts"][0]["url"], content=bytes(data))
        part.raise_for_status()
        request = dict(
            parts=[dict(part_number=1, etag=part.headers["ETag"])], idempotency_key="damaged-input"
        )
        assert (
            client.post("/v1/uploads/" + session["upload_id"] + "/complete", json=request).status_code
            == 422
        )
        state = client.get("/v1/assets/" + session["asset_id"]).json()
        assert state["status"] == "UPLOADING" and state["source_artifact_id"] is None
        assert not client.get("/v1/assets/" + session["asset_id"] + "/processing-runs").json()["items"]
