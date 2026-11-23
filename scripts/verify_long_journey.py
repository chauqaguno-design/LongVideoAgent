"""Run an authenticated two-hour sparse-source editing journey on an isolated local API."""

import argparse
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import secrets
import subprocess
from tempfile import TemporaryDirectory
import time

from fastapi.testclient import TestClient
from PIL import Image
from dovideo.presentation.api.app import create_app
from dovideo.presentation.composition import AnalysisSettings


QUOTE = "片尾测试标记黄灯亮起"
TRUTH = {"start_us": 7_170_000_000, "end_us": 7_172_000_000}


def wait_for(fetch, terminal, timeout=300):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        item = fetch()
        if item and item["status"] in terminal:
            return item
        time.sleep(0.2)
    raise TimeoutError("Timed out waiting for the local worker")


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf8")


def run(client, source, output, bins):
    def api(method, path, **kwargs):
        response = client.request(method, "/v1" + path, **kwargs)
        response.raise_for_status()
        return response.json()

    def post(path, **body):
        return api("POST", path, json=body)

    def artifact(key):
        item = api("GET", "/artifacts/" + key)
        data = client.get(item["url"])
        data.raise_for_status()
        assert hashlib.sha256(data.content).hexdigest() == item["sha256"]
        return data.content, item["sha256"]

    def events(run_id, last=None):
        headers = {"Last-Event-ID": last} if last else {}
        response = client.get(f"/v1/workflow-runs/{run_id}/events", headers=headers)
        response.raise_for_status()
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    def workflow(job):
        url = "/workflow-runs/" + job["run_id"]
        state = wait_for(lambda: api("GET", url), {"SUCCEEDED", "AWAITING_INPUT", "FAILED", "CANCELLED"})
        assert state["status"] == "SUCCEEDED", state
        assert state["budget_usage"]["model_calls"] == 0
        return state, api("GET", url + "/result")

    creds = dict(username="journey" + secrets.token_hex(4), password=secrets.token_urlsafe(18))
    client.post("/user/register", json=dict(creds, nickname="两小时测试")).raise_for_status()
    token = client.post("/user/login", json=creds).json()["data"]["token"]
    client.headers["Authorization"] = "Bearer " + token
    pid = post("/projects", title="时长两小时的稀疏素材验证")["project_id"]
    content = source.read_bytes()
    digest = hashlib.sha256(content).hexdigest()
    upload = post(
        f"/projects/{pid}/assets",
        filename=source.name,
        content_type="video/x-matroska",
        size_bytes=len(content),
    )
    put = client.put(upload["parts"][0]["url"], content=content)
    put.raise_for_status()
    asset = post(
        f"/uploads/{upload['upload_id']}/complete",
        idempotency_key="long-journey-upload",
        parts=[dict(part_number=1, etag=put.headers["etag"])],
    )
    aid = asset["asset_id"]
    scope = dict(project_id=pid, media_revision_ids=[asset["media_revision_id"]])
    started = time.monotonic()

    def processing():
        items = api("GET", f"/assets/{aid}/processing-runs")["items"]
        return items[0] if items else None

    job = wait_for(processing, {"SUCCEEDED", "FAILED", "PARTIAL"})
    assert job["status"] in {"SUCCEEDED", "PARTIAL"}, job
    scope_time = time.monotonic() - started
    assert api("GET", f"/assets/{aid}")["duration_us"] == 7_200_000_000

    text = dict(scope=scope, text=f"字幕：「{QUOTE}」", query_mode="LOCAL", top_k=10)
    found = post("/search", **text)
    text_run, answer = workflow(
        post(f"/projects/{pid}/search-workflows", search=text, idempotency_key="late-dialogue")
    )
    evidence = [
        api("GET", "/evidence/" + e)
        for c in answer["claims"]
        if c["support"] == "SUPPORTED"
        for e in c["evidence_ids"]
    ]
    assert any(e["range"] == TRUTH and QUOTE in e["claim"] for e in evidence), (answer, evidence, job)
    cursor = None
    frames = []
    while True:
        params = dict(limit=100)
        if cursor:
            params["cursor"] = cursor
        page = api("GET", f"/assets/{aid}/coverage", params=params)
        for entry in page["items"]:
            if entry["range"]["start_us"] >= 6_900_000_000 and entry["frame_artifact_ids"]:
                frames.extend(entry["frame_artifact_ids"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert frames, "The sample budget must include a late source frame"
    picture, picture_sha = artifact(frames[-1])
    with Image.open(BytesIO(picture)) as rgb:
        r, g, b = rgb.convert("RGB").resize((1, 1)).getpixel((0, 0))
        assert r > 200 and g > 200 and b < 50, (r, g, b)
    (output / "late-screenshot.jpg").write_bytes(picture)
    image = api("POST", f"/projects/{pid}/query-images", files={"file": ("late.jpg", picture, "image/jpeg")})
    iq = dict(scope=scope, image_artifact_id=image["artifact_id"], query_mode="LOCAL", top_k=8)
    image_hits = post("/search", **iq)
    image_run, visual = workflow(
        post(f"/projects/{pid}/search-workflows", search=iq, idempotency_key="late-picture")
    )
    visual_evidence = [
        api("GET", "/evidence/" + e)
        for c in visual["claims"]
        if c["support"] == "SUPPORTED"
        for e in c["evidence_ids"]
    ]
    assert any(
        p["range"]["start_us"] >= 6_900_000_000 and p["verification_protocol"].startswith("exact-rgb")
        for p in visual_evidence
    )
    instruction = "把第 0 到 2 秒和第 7170 到 7172 秒依次拼接并静音"
    requested = post(
        "/workflow-runs", scope=scope, intent="EDIT", message=instruction, idempotency_key="two-part-preview"
    )
    edit, plan = workflow(requested)
    replay = events(edit["run_id"], requested["last_event_id"])
    all_events = events(edit["run_id"])
    before = next(e for e in all_events if e["event_id"] == requested["last_event_id"])
    assert replay and replay == [e for e in all_events if e["sequence"] > before["sequence"]]
    assert replay[-1]["sequence"] == edit["last_event_sequence"]
    preview = api("GET", "/render-jobs/" + plan["render_job_id"])
    assert preview["status"] == "SUCCEEDED" and preview["profile"] == "PREVIEW"
    preview_data, preview_sha = artifact(preview["artifact_id"])
    (output / "preview.mp4").write_bytes(preview_data)
    edit_path = "/edit-projects/" + plan["edit_project_id"]
    rev1 = api("GET", edit_path + "/revisions/" + plan["revision_id"])
    first, last = rev1["clips"]
    assert last["source_start_us"] == TRUTH["start_us"]
    trimmed = {k: v for k, v in first.items() if k != "clip_id"}
    trimmed["source_end_us"] = 1_520_000
    ops = [
        dict(op="replace_clip", clip_id=first["clip_id"], clip=trimmed),
        dict(op="reorder_clips", clip_ids=[last["clip_id"], first["clip_id"]]),
    ]
    patch = dict(expected_revision=rev1["revision_no"], reason="先看片尾黄灯，然后回到片头", operations=ops)
    stale = client.post(
        "/v1" + edit_path + "/operations",
        json=dict(patch, expected_revision=0, idempotency_key="stale-order"),
    )
    assert stale.status_code == 409, stale.text
    rev2 = post(edit_path + "/operations", **patch, idempotency_key="trim-and-reorder")
    updated = api("GET", edit_path + "/revisions/" + rev2["revision_id"])
    assert [c["clip_id"] for c in updated["clips"]] == [last["clip_id"], first["clip_id"]]
    assert updated["clips"][-1]["source_end_us"] == 1_520_000
    result = post(
        "/render-jobs", revision_id=rev2["revision_id"], profile="EXPORT", idempotency_key="final-export"
    )
    final = wait_for(
        lambda: api("GET", "/render-jobs/" + result["render_job_id"]), {"SUCCEEDED", "FAILED", "CANCELLED"}
    )
    assert final["status"] == "SUCCEEDED" and final["revision_id"] == rev2["revision_id"], final
    final_data, final_sha = artifact(final["artifact_id"])
    path = output / "export.mp4"
    path.write_bytes(final_data)
    validation = json.loads(artifact(final["validation_report_id"])[0])
    assert validation["full_decode"] and validation["duration_us"] == 3_520_000, validation
    subprocess.run(
        [bins.ffmpeg_executable, "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"], check=True
    )

    def pixel(at):
        return list(
            subprocess.check_output(
                [
                    bins.ffmpeg_executable,
                    "-v",
                    "error",
                    "-ss",
                    str(at),
                    "-i",
                    str(path),
                    "-frames:v",
                    "1",
                    "-vf",
                    "scale=1:1",
                    "-f",
                    "rawvideo",
                    "-pix_fmt",
                    "rgb24",
                    "-",
                ]
            )
        )

    pixels = [pixel(0.4), pixel(2.4)]
    assert pixels[0][0] > 200 and pixels[0][1] > 200 and pixels[0][2] < 50 and max(pixels[1]) < 15, pixels
    write(output / "validation.json", validation)
    return dict(
        fixture="Generated, silent, 480-frame 1/15 fps sparse H.264 source; yellow from 6900 s. Embedded test subtitle added without re-encoding video.",
        source_sha256=digest,
        duration_us=7_200_000_000,
        source_bytes=len(content),
        ground_truth=TRUTH,
        project=pid,
        media_revision=asset["media_revision_id"],
        generation=job["index_generation_id"],
        processing=job,
        processing_seconds=round(scope_time, 3),
        capabilities=api("GET", f"/assets/{aid}")["capabilities"],
        query_ids=[found["query_id"], image_hits["query_id"]],
        text_run=text_run["run_id"],
        text_proof=evidence,
        image_run=image_run["run_id"],
        screenshot_sha256=picture_sha,
        image_proof=visual_evidence,
        edit_instruction=instruction,
        edit_run=edit["run_id"],
        preview=dict(revision=rev1["revision_id"], artifact=preview["artifact_id"], sha256=preview_sha),
        replay=dict(
            last_event_id=before["event_id"],
            resume_from_sequence=before["sequence"],
            last_sequence=replay[-1]["sequence"],
            events_replayed=len(replay),
        ),
        stale_edit_response=stale.status_code,
        patched_ranges=[(c["source_start_us"], c["source_end_us"]) for c in updated["clips"]],
        export=dict(
            revision=rev2["revision_id"],
            artifact=final["artifact_id"],
            render_job=final["render_job_id"],
            sha256=final_sha,
            validation_report=final["validation_report_id"],
            duration_us=validation["duration_us"],
            full_decode=True,
            sample_rgb=pixels,
        ),
        workflow_usage=[r["budget_usage"] for r in (text_run, image_run, edit)],
        remote_model_calls=0,
        limits="Tests public APIs with generated sparse media and embedded captions. Timing does not measure dense feature-film processing.",
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sparse_source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for name in ("VIDEOMIND_SEARCH_ENCODER_URL", "DOVIDEO_PADDLE_OCR_DIR", "DOVIDEO_WHISPER_DIR"):
        os.environ.pop(name, None)
    os.environ.update(
        DOVIDEO_PROFILE="local",
        DOVIDEO_MEDIA_REMOTE_FLASH="0",
        DOVIDEO_LOCAL_FLASH_INFERENCE="0",
        DOVIDEO_WORKFLOW_LIVE_MODEL="0",
        DOVIDEO_WORKFLOW_TEAM="true",
        DOVIDEO_MAX_MEDIA_FRAMES="12",
    )
    bins = AnalysisSettings.from_environment()
    srt = args.output / "ground-truth.srt"
    srt.write_text("1\n01:59:30,000 --> 01:59:32,000\n" + QUOTE + "\n", encoding="utf8")
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        source = root / "long-fixture.mkv"
        subprocess.run(
            [
                bins.ffmpeg_executable,
                "-v",
                "error",
                "-copyts",
                "-i",
                str(args.sparse_source),
                "-i",
                str(srt),
                "-map",
                "0:v",
                "-map",
                "1:s",
                "-c:v",
                "copy",
                "-c:s",
                "srt",
                str(source),
            ],
            check=True,
        )
        embedded = subprocess.check_output(
            [
                bins.ffmpeg_executable,
                "-v",
                "error",
                "-copyts",
                "-i",
                str(source),
                "-map",
                "0:s:0",
                "-f",
                "srt",
                "-",
            ]
        )
        assert "01:59:30,000 --> 01:59:32,000" in embedded.decode("utf-8-sig")
        with TestClient(create_app(work_dir=root / "service")) as client:
            report = run(client, source, args.output, bins)
        with args.sparse_source.open("rb") as sparse:
            report["original_input_sha256"] = hashlib.file_digest(sparse, "sha256").hexdigest()
    write(args.output / "journey.json", report)
    print("Two-hour source reached a validated, independently decoded reordered export.")


if __name__ == "__main__":
    main()
