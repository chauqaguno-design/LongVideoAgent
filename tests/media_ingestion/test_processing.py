"""Check real decoded frames, proxy mapping, atomic publication and revocation."""

from hashlib import sha256
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient
from cutsieve.application.media_sampling import sample_points, segments, windows
from cutsieve.presentation.api.app import create_app


def test_plan_spreads_base_budget_across_two_hours_and_covers_tail():
    spans = windows(7_200_123_456)
    assert len(spans) == 121 and spans[-1] == (7_200_000_000, 7_200_123_456)
    plan = [sample_points(start, end, slot) for slot in range(4) for start, end in spans]
    assert len(set(plan)) == 4 * len(spans)
    assert plan[120] > 7_200_000_000
    chunks = list(segments(23_000_000))
    assert chunks == [(0, 10_000_000), (8_000_000, 18_000_000), (16_000_000, 23_000_000)]


@pytest.mark.real_media
def test_real_proxy_subtitle_frames_replay_and_revocation(tmp_path, monkeypatch, use_workspace):
    exe = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    for setting in ("CUTSIEVE_WHISPER_DIR", "CUTSIEVE_PADDLE_OCR_DIR"):
        monkeypatch.delenv(setting, raising=False)
    srt = tmp_path / "words.srt"
    srt.write_text(
        "1\n00:00:00,500 --> 00:00:01,200\n打开红门\n\n2\n00:00:01,800 --> 00:00:02,200\n打开红门\n",
        encoding="utf8",
    )
    movie = tmp_path / "clip.mkv"
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x48:r=8:d=3",
            "-i",
            str(srt),
            "-c:v",
            "ffv1",
            "-c:s",
            "srt",
            str(movie),
        ],
        check=True,
    )
    with TestClient(create_app(work_dir=tmp_path / "app")) as http:
        project = http.post("/v1/projects", json=dict(title="影片")).json()["project_id"]
        data = movie.read_bytes()
        session = http.post(
            f"/v1/projects/{project}/assets",
            json=dict(filename="clip.mkv", content_type="video/x-matroska", size_bytes=len(data)),
        ).json()
        etag = http.put(session["parts"][0]["url"], content=data).headers["ETag"]
        completion = dict(parts=[dict(part_number=1, etag=etag)], idempotency_key="ingest-example")
        url = f"/v1/uploads/{session['upload_id']}/complete"
        asset = http.post(url, json=completion).json()
        aid = asset["asset_id"]
        assert http.post(url, json=completion).json()["asset_id"] == aid
        runs = http.get(f"/v1/assets/{aid}/processing-runs").json()["items"]
        assert len(runs) == 1
        run_id = runs[0]["processing_run_id"]
        for _ in range(200):
            run = http.get(f"/v1/processing-runs/{run_id}").json()
            if run["status"] not in ("QUEUED", "RUNNING"):
                break
            time.sleep(0.15)
        assert run["status"] == "PARTIAL", run
        revision = http.get("/v1/media-revisions/" + asset["media_revision_id"]).json()
        assert revision["playback_artifact_id"] != revision["source_artifact_id"]
        proxy = http.get("/v1/artifacts/" + revision["playback_artifact_id"]).json()
        assert sha256(http.get(proxy["url"]).content).hexdigest() == proxy["sha256"]
        mapping = http.get("/v1/artifacts/" + revision["proxy_time_mapping_artifact_id"]).json()
        assert http.get(mapping["url"]).json()["source_duration_us"] == 3_000_000
        obs = http.get(f"/v1/assets/{aid}/observations").json()["items"]
        assert len(obs) == 2 and {r["text"] for r in obs} == {"打开红门"}
        assert sorted(r["range"]["start_us"] for r in obs) == [500000, 1800000]
        cov = http.get(f"/v1/assets/{aid}/coverage").json()["items"]
        assert {c["stage"] for c in cov if c["modality"] == "VISUAL"} == {"PLANNED", "DECODED"}
        assert any(c["modality"] == "OCR" and c["stage"] == "FAILED" for c in cov)
        frames = next(c for c in cov if c["modality"] == "VISUAL" and c["stage"] == "DECODED")[
            "frame_artifact_ids"
        ]
        assert len(frames) >= 4
        frame = http.get("/v1/artifacts/" + frames[0]).json()
        assert http.get(frame["url"]).content.startswith(b"\xff\xd8")
        notes_id = http.get(f"/v1/assets/{aid}/notes").json()["artifact_id"]
        notes_file = http.get("/v1/artifacts/" + notes_id).json()
        notes = http.get(notes_file["url"]).json()
        assert len(notes["chapters"][0]["claims"]) == 2
        tracks = http.get(f"/v1/assets/{aid}/caption-track").json()
        assert tracks["media_revision_id"] == asset["media_revision_id"]
        track = http.get("/v1/artifacts/" + tracks["caption_track_id"]).json()
        assert "打开红门" in http.get(track["url"]).content.decode("utf8")
        verify_caption_render(http, project, tracks, tmp_path, exe, monkeypatch)
        metrics = http.get("/v1/readiness").json()
        assert metrics["ready"] and metrics["queues"]["render"]["counts"]["SUCCEEDED"] >= 1
        assert metrics["uncertain_model_requests"] == 0
        cat = http.app.state.processing.catalog
        scope = asset["workspace_id"]
        first_gen, rows = cat.search_scope(scope, project, [asset["media_revision_id"]])
        assert first_gen == run["index_generation_id"]
        assert len([r for r in rows if r["claim"] == "打开红门"]) == 2
        assert cat.literal_coverage(scope, project, first_gen, [asset["media_revision_id"]]) == {
            asset["media_revision_id"]: [dict(start_us=0, end_us=3_000_000)]
        }
        previews = f"/v1/projects/{project}/evidence-previews"
        proof = next(r["evidence_id"] for r in rows if r["claim"] == "打开红门")
        item = http.post(previews, json=[proof]).json()[proof]
        assert item["text"] == "打开红门" and http.get(item["thumbnail"]).status_code == 200
        with use_workspace(http, 2):
            assert http.get("/v1/readiness").json()["queues"]["render"]["counts"] == {}
            assert http.get(f"/v1/assets/{aid}/caption-track").status_code == 404
            assert http.post(previews, json=[proof]).status_code == 404
        reply = http.delete(f"/v1/assets/{aid}")
        assert reply.status_code == 202
        assert http.get(frame["url"]).status_code == 404
        assert http.get(proxy["url"]).status_code == 404
        assert http.get(track["url"]).status_code == 404
        assert http.get(item["thumbnail"]).status_code == 404
        assert http.get("/v1/media-revisions/" + asset["media_revision_id"]).status_code == 404
        assert http.get(f"/v1/projects/{project}/assets").json()["items"] == []


def verify_caption_render(http, project_id, track, folder, ffmpeg, monkeypatch):
    def post(path, **payload):
        response = http.post("/v1" + path, json=payload)
        response.raise_for_status()
        return response.json()

    profile = dict(width=96, height=64, fps_num=25, fps_den=1, audio_sample_rate=48000)
    edit = post("/edit-projects", project_id=project_id, title="字幕验证", output_profile=profile,
                idempotency_key="caption-project")
    clip = dict(track, source_start_us=400_000, source_end_us=1_000_000, audio_policy="keep", evidence_ids=[])
    ops = "/edit-projects/" + edit["edit_project_id"] + "/operations"
    patch = dict(expected_revision=0, operations=[dict(op="append_clip", clip=clip),
                 dict(op="set_caption_policy", caption_policy="preserve_source")], reason="保留对齐字幕",
                 idempotency_key="caption-preserve")
    revision = post(ops, **patch)["revision_id"]

    def render(rev, name):
        job = post("/render-jobs", revision_id=rev, profile="PREVIEW", idempotency_key=name)
        for _ in range(200):
            job = http.get("/v1/render-jobs/" + job["render_job_id"]).json()
            if job["status"] in {"FAILED", "SUCCEEDED"}:
                break
            time.sleep(.1)
        assert job["status"] == "SUCCEEDED", job
        artifact = http.get("/v1/artifacts/" + job["artifact_id"]).json()
        movie = folder / (name + ".mp4")
        movie.write_bytes(http.get(artifact["url"]).content)
        return movie

    soft = render(revision, "caption-soft")
    args = [ffmpeg, "-v", "error", "-i"]
    subs = subprocess.check_output([*args, str(soft), "-map", "0:s:0", "-f", "srt", "pipe:1"]).decode()
    assert "00:00:00,100 --> 00:00:00,600" in subs and "打开红门" in subs
    revision = post(ops, expected_revision=1, reason="烧录字幕",
                    operations=[dict(op="set_caption_policy", caption_policy="burn_aligned")],
                    idempotency_key="caption-burn")["revision_id"]
    font = next((p for p in [os.getenv("CUTSIEVE_CAPTION_FONT"), "C:/Windows/Fonts/simhei.ttf",
                             "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"] if p and Path(p).is_file()), None)
    monkeypatch.delenv("CUTSIEVE_CAPTION_FONT", raising=False)
    missing = http.post("/v1/render-jobs", json=dict(revision_id=revision, profile="PREVIEW",
                                                      idempotency_key="font-unavailable"))
    assert missing.status_code == 503
    if font:
        monkeypatch.setenv("CUTSIEVE_CAPTION_FONT", font)
        burned = render(revision, "caption-burned")
        def frame(movie, when):
            return subprocess.check_output([*args, str(movie), "-ss", when, "-frames:v", "1",
                                            "-pix_fmt", "gray", "-f", "rawvideo", "pipe:1"])
        # Burned characters change source pixels inside the cue interval.
        before, after = frame(soft, "0.3"), frame(burned, "0.3")
        assert len(before) == len(after) == 96 * 64
        assert sum(abs(a - b) > 25 for a, b in zip(before, after)) > 15
