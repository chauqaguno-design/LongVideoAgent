"""The normal chat API should work across turns without a client-supplied intent or evidence ID."""

import os
import shutil
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from dovideo.presentation.api.app import create_app


def post(client, path, **body):
    response = client.post(path, json=body)
    response.raise_for_status()
    return response.json()


def login(client, username):
    credentials = dict(username=username, password="local-chat-tests")
    post(client, "/user/register", **credentials, nickname="test")
    client.headers["Authorization"] = "Bearer " + post(client, "/user/login", **credentials)["data"]["token"]


def upload(client, project, file):
    path = f"/v1/projects/{project}/assets"
    data = file.read_bytes()
    upload = post(client, path, filename=file.name, size_bytes=len(data), content_type="video/mp4")
    part = client.put(upload["parts"][0]["url"], content=data)
    part.raise_for_status()
    return post(
        client,
        "/v1/uploads/" + upload["upload_id"] + "/complete",
        parts=[dict(part_number=1, etag=part.headers["ETag"])],
        idempotency_key="complete-chat-fixture",
    )["media_revision_id"]


def wait(client, run_id, status="SUCCEEDED"):
    path = "/v1/workflow-runs/" + run_id
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = client.get(path).json()
        if state["status"] in ("SUCCEEDED", "FAILED", "AWAITING_INPUT", "CANCELLED"):
            assert state["status"] == status, state
            return state
        time.sleep(0.1)
    pytest.fail(str(state))


@pytest.fixture
def source(tmp_path):
    ffmpeg = shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    captions = tmp_path / "quote.srt"
    captions.write_text(
        "1\n00:00:01,000 --> 00:00:02,000\nTicket\n\n" "2\n00:00:04,000 --> 00:00:05,000\nTicket\n",
        encoding="utf8",
    )
    movie = tmp_path / "train.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=80x64:r=10:d=6",
            "-i",
            str(captions),
            "-map",
            "0:v",
            "-map",
            "1:s",
            "-c:v",
            "libx264",
            "-c:s",
            "mov_text",
            str(movie),
        ],
        check=True,
    )
    return movie


@pytest.mark.real_media
def test_find_second_occurrence_preview_extend_export_and_restore(tmp_path, monkeypatch, source):
    monkeypatch.setenv("DOVIDEO_WORKFLOW_LIVE_MODEL", "0")
    monkeypatch.delenv("DOVIDEO_WORKFLOW_TEAM", raising=False)
    folder = tmp_path / "app"
    with TestClient(create_app(work_dir=folder)) as client:
        login(client, "ticket_editor")
        project = post(client, "/v1/projects", title="Train")["project_id"]
        media = upload(client, project, source)
        root = f"/v1/projects/{project}/conversation-runs"

        def say(text, parent=None, key=None):
            return post(
                client,
                root,
                message=text,
                media_revision_id=media,
                parent_run_id=parent,
                idempotency_key=key or str(uuid4()),
            )["run_id"]

        def turns(run):
            return client.get(f"/v1/workflow-runs/{run}/conversation").json()["turns"]

        found = say("哪一段说了「Ticket」？", key="find-ticket")
        assert say("哪一段说了「Ticket」？", key="find-ticket") == found
        wait(client, found)
        (first,) = turns(found)
        assert first["route"]["plan"] == ["SEARCH", "ANSWER"]
        assert [c["range"]["start_us"] for c in first["candidates"]] == [1_000_000, 4_000_000]
        assert not client.get(f"/v1/projects/{project}/edit-projects").json()["items"]

        preview = say("把刚才第二段剪出来", found)
        wait(client, preview)
        previous, current = turns(preview)
        assert previous["run_id"] == found and "PREVIEW" in current["route"]["plan"]
        result = client.get(f"/v1/workflow-runs/{preview}/result").json()
        edited = f"/v1/edit-projects/{result['edit_project_id']}/revisions/"
        (clip,) = client.get(edited + result["revision_id"]).json()["clips"]
        assert (clip["source_start_us"], clip["source_end_us"]) == (4_000_000, 5_000_000)
        assert clip["evidence_ids"] == first["candidates"][1]["evidence_ids"]

        final = say("前面再留两秒，然后导出", preview)
        wait(client, final)
        result = client.get(f"/v1/workflow-runs/{final}/result").json()
        assert result["export_artifact_id"]
        (changed,) = client.get(edited + result["revision_id"]).json()["clips"]
        assert (changed["source_start_us"], changed["source_end_us"]) == (2_000_000, 5_000_000)
        assert changed["clip_id"] == clip["clip_id"]
        formats = say("这个导出只能导出 MP4 吗？", final)
        assert wait(client, formats)["budget_usage"]["tool_calls"] == 0
        assert "WEBM" in turns(formats)[-1]["answer"]
        webm = say("把上一版导出为 WebM", formats)
        wait(client, webm)
        webm_result = client.get(f"/v1/workflow-runs/{webm}/result").json()
        assert webm_result["revision_id"] == result["revision_id"]
        assert webm_result["export_format"] == "webm"
        artifact = client.get("/v1/artifacts/" + webm_result["export_artifact_id"]).json()
        assert artifact["mime_type"] == "video/webm"
        assert client.get(artifact["url"]).content.startswith(b"\x1aE\xdf\xa3")
        creds = dict(username="ticket_editor", password="local-chat-tests")

        ambiguous = say("把刚才那段剪出来", found)
        state = wait(client, ambiguous, "AWAITING_INPUT")
        post(
            client,
            f"/v1/workflow-runs/{ambiguous}/resume",
            expected_state_version=state["state_version"],
            message="第 2 段",
            idempotency_key="choose-second",
        )
        wait(client, ambiguous)
        assert turns(ambiguous)[-1]["clarifications"][-1]["reply"] == "第 2 段"

        unbound = say("把刚才那段剪出来")
        state = wait(client, unbound, "AWAITING_INPUT")
        post(
            client,
            f"/v1/workflow-runs/{unbound}/resume",
            expected_state_version=state["state_version"],
            message="剪出第 2 到 3 秒",
            idempotency_key="supply-absolute-range",
        )
        wait(client, unbound)
        result = client.get(f"/v1/workflow-runs/{unbound}/result").json()
        revision = client.get(f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}").json()
        (clip,) = revision["clips"]
        assert (clip["source_start_us"], clip["source_end_us"]) == (2_000_000, 3_000_000)

        other = post(client, "/v1/projects", title="Other")["project_id"]
        second_media = upload(client, other, source)
        foreign_body = dict(
            message="把第二段剪出来",
            media_revision_id=second_media,
            parent_run_id=found,
            idempotency_key="bad-parent",
        )
        assert client.post(f"/v1/projects/{other}/conversation-runs", json=foreign_body).status_code == 404
        login(client, "other_editor")
        assert client.get(f"/v1/workflow-runs/{final}/conversation").status_code == 404

    with TestClient(create_app(work_dir=folder)) as restored:
        restored.headers["Authorization"] = (
            "Bearer " + post(restored, "/user/login", **creds)["data"]["token"]
        )
        view = restored.get(f"/v1/workflow-runs/{final}/conversation").json()
        assert [r["run_id"] for r in view["turns"]] == [found, preview, final]
