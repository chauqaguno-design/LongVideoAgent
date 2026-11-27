"""Two workspace scopes import visually identical media using the local worker and API."""
import os
import shutil
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from cutsieve.presentation.api.app import create_app


def json_body(response):
    response.raise_for_status()
    return response.json()


def wait_for(client, url, active):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        state = json_body(client.get(url))
        if state["status"] not in active:
            return state
        time.sleep(0.08)
    pytest.fail(f"Job did not finish: {url}")


def prepare_workspace(client, folder, ffmpeg, name):
    project = json_body(client.post("/v1/projects", json={"title": name}))["project_id"]
    caption, movie = folder / f"{name}.srt", folder / f"{name}.mp4"
    caption.write_text(f"1\n00:00:00,100 --> 00:00:00,800\nShared ticket {name}\n", encoding="utf8")
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=blue:s=96x64:r=10:d=1",
                    "-i", str(caption), "-c:v", "libx264", "-c:s", "mov_text", str(movie)], check=True)
    data = movie.read_bytes()
    upload = json_body(client.post(f"/v1/projects/{project}/assets", json=dict(
        filename="story.mp4", size_bytes=len(data), content_type="video/mp4")))
    upload = json_body(client.post(f"/v1/uploads/{upload['upload_id']}/refresh", json=dict(
        part_numbers=[1], idempotency_key="refresh-part")))
    part = client.put(upload["parts"][0]["url"], content=data)
    part.raise_for_status()
    asset = json_body(client.post(f"/v1/uploads/{upload['upload_id']}/complete", json=dict(
        parts=[dict(part_number=1, etag=part.headers["ETag"])], idempotency_key="same-upload-key")))
    media = asset["media_revision_id"]
    aid = asset["asset_id"]
    pid = json_body(client.get(f"/v1/assets/{aid}/processing-runs"))["items"][0]["processing_run_id"]
    processing = wait_for(client, f"/v1/processing-runs/{pid}", {"QUEUED", "RUNNING"})
    assert processing["status"] in {"PARTIAL", "SUCCEEDED"}
    scope = dict(project_id=project, media_revision_ids=[media])
    query = dict(scope=scope, text="Shared ticket", top_k=5, query_mode="LOCAL")
    hits = json_body(client.post("/v1/search", json=query))["hits"]
    assert hits and {h["media_revision_id"] for h in hits} == {media}
    proof = json_body(client.get("/v1/evidence/" + hits[0]["evidence_ids"][0]))
    observation = proof["observation_ids"][0]
    assert json_body(client.get("/v1/observations/" + observation))["media_revision_id"] == media
    coverage = json_body(client.get(f"/v1/assets/{aid}/coverage"))["items"]
    frame = next(r["frame_artifact_ids"][0] for r in coverage if r["frame_artifact_ids"])
    meta = json_body(client.get("/v1/artifacts/" + frame))
    frame_bytes = client.get(meta["url"]).content
    query_image = json_body(client.post(f"/v1/projects/{project}/query-images",
        files={"file": ("frame.png", frame_bytes, "image/png")}))
    run = json_body(client.post("/v1/workflow-runs", json=dict(
        scope=scope, intent="EDIT", message="剪出 0 到 0.5 秒并导出",
        idempotency_key="same-edit-key")))["run_id"]
    base = "/v1/workflow-runs/" + run
    state = wait_for(client, base, {"QUEUED", "RUNNING", "WAITING_JOB"})
    assert state["status"] == "SUCCEEDED", state
    edit = json_body(client.get(base + "/result"))
    return dict(project=project, asset=aid, upload=upload["upload_id"], media=media,
                evidence=proof["evidence_id"], observation=observation, frame=frame,
                image=query_image["artifact_id"], run=run, result=state["result_artifact_id"],
                edit=edit["edit_project_id"], revision=edit["revision_id"], job=edit["export_job_id"],
                export=edit["export_artifact_id"], processing=pid, scope=scope)


RESOURCE_PATHS = (
    "/projects/{project}/assets", "/projects/{project}/workflow-runs", "/projects/{project}/edit-projects",
    "/uploads/{upload}", "/assets/{asset}", "/assets/{asset}/processing-runs", "/assets/{asset}/coverage",
    "/assets/{asset}/observations", "/processing-runs/{processing}", "/media-revisions/{media}",
    "/observations/{observation}", "/evidence/{evidence}", "/artifacts/{frame}", "/artifacts/{image}",
    "/artifacts/{export}", "/artifacts/{result}", "/workflow-runs/{run}", "/workflow-runs/{run}/result",
    "/workflow-runs/{run}/events",
    "/workflow-runs/{run}/jobs/{job}", "/edit-projects/{edit}", "/edit-projects/{edit}/revisions",
    "/edit-projects/{edit}/revisions/{revision}", "/edit-projects/{edit}/render-jobs", "/render-jobs/{job}",
)


def error_body(response):
    assert response.status_code == 404, (response.request.url.path, response.text)
    body = response.json()
    assert body.pop("request_id")
    return body


def scoped_writes(client, resources):
    """Each proposed change uses a documented request and a scoped target."""
    def post(path, body):
        return client.post("/v1" + path.format(**resources), json=body)

    stop = dict(idempotency_key="scope-check")
    return [
        post("/uploads/{upload}/refresh", dict(stop, part_numbers=[1])),
        post("/assets/{asset}/processing-runs", dict(stop, requested_capabilities=["ocr"])),
        post("/processing-runs/{processing}/cancel", stop),
        post("/workflow-runs/{run}/resume", dict(stop, expected_state_version=1, message="continue")),
        post("/workflow-runs/{run}/cancel", stop),
        post("/edit-projects/{edit}/operations", dict(stop, expected_revision=0, reason="test edit",
             operations=[dict(op="set_caption_policy", caption_policy="none")])),
        post("/render-jobs", dict(stop, profile="EXPORT", revision_id=resources["revision"])),
        post("/render-jobs/{job}/cancel", stop),
        client.delete("/v1/assets/" + resources["asset"]),
    ]


def exercise_controls(client, resources):
    """Use a separate draft and an awaiting-input run so both can be changed safely."""
    json_body(client.get("/v1/capabilities"))
    json_body(client.get("/v1/projects"))
    create = dict(project_id=resources["project"], title="Cut", idempotency_key="create-timeline",
                  output_profile=dict(width=96, height=64, fps_num=10, fps_den=1, audio_sample_rate=48000))
    edit = json_body(client.post("/v1/edit-projects", json=create))["edit_project_id"]
    clip = dict(media_revision_id=resources["media"], source_start_us=0, source_end_us=500_000,
                crop=dict(x=0, y=0, width=32, height=32), audio_policy="keep", evidence_ids=[])
    patch = dict(expected_revision=0, idempotency_key="append-one-cut", reason="use the first half",
                 operations=[dict(op="append_clip", clip=clip)])
    receipt = json_body(client.post(f"/v1/edit-projects/{edit}/operations", json=patch))
    assert json_body(client.post(f"/v1/edit-projects/{edit}/operations", json=patch))["replayed"]
    job = json_body(client.post("/v1/render-jobs", json=dict(
        revision_id=receipt["revision_id"], profile="EXPORT", idempotency_key="render-one-cut")))
    stop = dict(idempotency_key="stop-current-job")
    json_body(client.post(f"/v1/render-jobs/{job['render_job_id']}/cancel", json=stop))
    request = dict(scope=resources["scope"], intent="EDIT", message="帮我剪一下",
                   idempotency_key="ask-for-clarification")
    run = json_body(client.post("/v1/workflow-runs", json=request))
    base = "/v1/workflow-runs/" + run["run_id"]
    assert json_body(client.get(base + "/harness"))["harness_version"] == 1
    waiting = wait_for(client, base, {"QUEUED", "RUNNING", "WAITING_JOB"})
    assert waiting["status"] == "AWAITING_INPUT"
    adjust = dict(expected_state_version=waiting["state_version"], idempotency_key="extend-time",
                  new_limits=dict(waiting["budget_usage"]["limits"], deadline_seconds=1200), reason="more time")
    adjusted = json_body(client.patch(base + "/budget", json=adjust))
    json_body(client.post(base + "/resume", json=dict(expected_state_version=adjusted["state_version"],
              idempotency_key="continue-with-range", message="剪出 0 到 0.5 秒")))
    json_body(client.post(base + "/cancel", json=stop))
    processing = json_body(client.post(f"/v1/assets/{resources['asset']}/processing-runs",
        json=dict(idempotency_key="read-captions-again", requested_capabilities=["ocr"])))
    json_body(client.post(f"/v1/processing-runs/{processing['processing_run_id']}/cancel", json=stop))
    json_body(client.delete("/v1/assets/" + resources["asset"]))


@pytest.mark.real_media
def test_two_workspaces_share_pixels_and_request_keys_but_only_read_their_results(tmp_path, monkeypatch, use_workspace):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_LIVE_MODEL", "0")
    with TestClient(create_app(work_dir=tmp_path / "app")) as client:
        one = prepare_workspace(client, tmp_path, ffmpeg, "first_editor")
        with use_workspace(client, 2):
            two = prepare_workspace(client, tmp_path, ffmpeg, "second_editor")
        for user_id, own, foreign in ((1, one, two), (2, two, one)):
            with use_workspace(client, user_id):
                unknown = {key: str(uuid4()) for key in foreign}
                for path in RESOURCE_PATHS:
                    own_response = client.get("/v1" + path.format(**own))
                    own_response.raise_for_status()
                    denied = client.get("/v1" + path.format(**foreign))
                    absent = client.get("/v1" + path.format(**unknown))
                    assert error_body(denied) == error_body(absent), path
                for denied, absent in zip(scoped_writes(client, foreign), scoped_writes(client, unknown), strict=True):
                    assert error_body(denied) == error_body(absent)
                # Both projects contain the same blue frames, indexed and cached by the local workers.
                image_search = dict(scope=own["scope"], image_artifact_id=own["image"], query_mode="LOCAL", top_k=5)
                hits = json_body(client.post("/v1/search", json=image_search))["hits"]
                assert hits and {h["media_revision_id"] for h in hits} == {own["media"]}
                for resource in ("image", "media"):
                    def query(key):
                        if resource == "image":
                            return dict(image_search, image_artifact_id=key)
                        return dict(image_search, scope=dict(project_id=own["project"], media_revision_ids=[key]))
                    for submit in (lambda q: client.post("/v1/search", json=q),
                                   lambda q: client.post(f"/v1/projects/{own['project']}/search-workflows",
                                        json=dict(search=q, idempotency_key="second-search"))):
                        assert error_body(submit(query(foreign[resource]))) == error_body(submit(query(unknown[resource])))
        exercise_controls(client, one)
