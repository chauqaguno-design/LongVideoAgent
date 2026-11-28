import json
from pathlib import Path
import shutil
import subprocess
import sys
from uuid import uuid4
import wave

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import text

from cutsieve.application.media_sampling import adaptive_points, static_duplicate
from cutsieve.infrastructure.edit_media import media_binaries, render_timeline
from cutsieve.infrastructure.media import processing_units
from cutsieve.infrastructure.media.local_inference import read_pcm16
from cutsieve.presentation.api.app import create_app
from cutsieve.presentation.api.capabilities import capabilities
from scripts.eval_harness import body, check_fades, decode, ingest, synthetic_video, wait_index


@pytest.fixture
def binaries():
    tools = media_binaries()
    if not all(shutil.which(exe) for exe in tools.values()):
        pytest.skip("FFmpeg and ffprobe required")
    return tools


def test_shot_midpoints_flicker_gap_cap_and_static_difference():
    assert adaptive_points(3_000_000, [], 0) == [1_500_000]
    assert adaptive_points(4_000_000, [200_000, 1_000_000, 1_300_000, 2_000_000], 100) == [
        500_000, 1_500_000, 3_000_000,
    ]
    assert adaptive_points(18_000_000, [], 100) == [2_250_000, 6_750_000, 11_250_000, 15_750_000]
    capped = adaptive_points(18_000_000, [], 2)
    assert len(capped) == 2 and capped[0] < 9_000_000 < capped[-1]
    signature = bytes(8 * 8 * 3)
    assert static_duplicate(signature, bytes([1]) * len(signature))
    assert not static_duplicate(signature, bytes([2]) * len(signature))


@pytest.mark.real_media
def test_scene_scan_and_fts_lazy_backfill_across_fresh_sessions(tmp_path, binaries, monkeypatch):
    for name in ("CUTSIEVE_WHISPER_DIR", "CUTSIEVE_WHISPER_MODEL_ROOT", "CUTSIEVE_SEARCH_ENCODER_URL",
                 "CUTSIEVE_MEDIA_AUTO_VISUAL"):
        monkeypatch.delenv(name, raising=False)
    path = synthetic_video(tmp_path)
    with TestClient(create_app(work_dir=tmp_path)) as client:
        project, asset = ingest(client, path, tmp_path)
        wait_index(client, asset, 25)
        catalog = client.app.state.processing.catalog
        workspace, mid = asset["workspace_id"], asset["media_revision_id"]
        _, rows = catalog.search_scope(workspace, project, [mid])
        frames = sorted(row["frame"]["actual_us"] for row in rows if row["source"] == "sampled-frame")
        assert frames[:7] == [750_000 + 1_500_000 * i for i in range(7)]
        assert len(frames) == 8
        with catalog.write() as db:
            assert db.execute(text("SELECT count(*) FROM media_evidence_fts WHERE evidence_id <> ''")).scalar() == 4
            db.execute(text("DROP TABLE media_evidence_fts"))

        for query, expected in (("机器人", "今天机器人飞向太空"), ("Scales", "The dragon is named Scales."),
                                ("黑衣人", "看见黑衣人走来")):
            result = body(client.post("/v1/search", json=dict(
                scope=dict(project_id=project), query_mode="LOCAL", text=query, top_k=1,
            )))
            hit, = result["hits"]
            assert hit["route_ranks"][0]["route"] == "fts5"
            evidence = body(client.get("/v1/evidence/" + hit["evidence_ids"][0]))
            assert evidence["claim"] == expected
        with catalog.sessions() as db:
            assert db.execute(text("SELECT count(*) FROM media_evidence_fts")).scalar() == 5
        client.delete(f"/v1/assets/{asset['asset_id']}").raise_for_status()
        with catalog.sessions() as db:
            assert db.execute(text("SELECT count(*) FROM media_evidence_fts")).scalar() == 0


@pytest.mark.real_media
def test_render_fades_audio_and_retains_muted_container_layout(tmp_path, binaries):
    movie = synthetic_video(tmp_path)
    mid = str(uuid4())
    rev = dict(
        revision_id=str(uuid4()), audio_fade_ms=300, caption_policy="none",
        output_profile=dict(width=160, height=90, fps_num=25, fps_den=1, audio_sample_rate=48000),
        clips=[dict(clip_id=str(uuid4()), media_revision_id=mid, source_start_us=start,
                    source_end_us=start + 1_200_000, audio_policy="keep") for start in (100_000, 2_000_000)],
    )
    mp4 = tmp_path / "fades.mp4"
    render_timeline(rev, {mid: movie}, mp4, job_id=str(uuid4()), **binaries)
    assert {s["codec_type"] for s in decode(mp4)} == {"video", "audio"}
    check_fades(mp4, (1.2, 1.2))
    for clip in rev["clips"]:
        clip["audio_policy"] = "mute"
    webm = tmp_path / "muted.webm"
    render_timeline(rev, {mid: movie}, webm, job_id=str(uuid4()), **binaries)
    assert [s["codec_type"] for s in decode(webm)] == ["video"]


def test_asr_readiness_uses_the_selected_interpreter(tmp_path, monkeypatch):
    root = tmp_path / "whisper"
    root.mkdir()
    env = {"CUTSIEVE_WHISPER_DIR": str(tmp_path / "missing"), "CUTSIEVE_WHISPER_MODEL_ROOT": str(root)}
    calls = []

    def command(args, stopped=None, **options):
        calls.append(args)
        assert "import faster_whisper" in args[-1]
        if args[0] == "wrong-python":
            raise ValueError("Import failed")
        return b"1.2.1\n", b""

    monkeypatch.setattr(processing_units, "command", command)
    service = lambda cfg: next(s for s in capabilities(cfg)["dedicated_services"] if s["name"] == "ASR")
    assert not service({})["ready"] and not calls
    assert service(env)["ready"] and calls[-1][0] == sys.executable
    assert not service(dict(env, CUTSIEVE_ASR_PYTHON="wrong-python"))["ready"]
    assert service(dict(CUTSIEVE_ASR_API_KEY="configured"))["ready"]


def test_whisper_adapter_reads_worker_pcm_without_pyav(tmp_path):
    wav = tmp_path / "audio.wav"
    samples = bytes.fromhex("0080 0000 ff7f")
    with wave.open(str(wav), "wb") as output:
        output.setparams((1, 2, 16_000, 0, "NONE", "not compressed"))
        output.writeframes(samples)
    assert read_pcm16(wav, 16_000) == samples
    with pytest.raises(ValueError, match="mono PCM16"):
        read_pcm16(wav, 48_000)


@pytest.mark.real_media
def test_unsubtitled_ingest_invokes_local_asr_and_indexes_transcript(tmp_path, binaries, monkeypatch):
    root = tmp_path / "whisper"
    root.mkdir()
    monkeypatch.setenv("CUTSIEVE_WHISPER_MODEL_ROOT", str(root))
    for name in ("CUTSIEVE_WHISPER_DIR", "CUTSIEVE_ASR_PYTHON", "CUTSIEVE_MEDIA_PYTHON", "CUTSIEVE_PADDLE_OCR_DIR",
                 "CUTSIEVE_SEARCH_ENCODER_URL", "CUTSIEVE_MEDIA_AUTO_VISUAL"):
        monkeypatch.delenv(name, raising=False)
    calls = []
    real_command = processing_units.command

    def command(args, stopped=None, **options):
        if args[0] != sys.executable:
            return real_command(args, stopped, **options)
        calls.append(args)
        if args[2] == "-c":
            assert "import faster_whisper" in args[-1]
            return b"1.2.1\n", b""
        assert Path(args[2]).name == "local_inference.py"
        spec = json.loads(Path(args[3]).read_text(encoding="utf-8"))
        assert spec["mode"] == "asr" and Path(spec["model"]) == root
        for item in spec["inputs"]:
            assert Path(item["path"]).read_bytes()[:4] == b"RIFF"
            Path(item["output"]).write_text(json.dumps(dict(
                model="faster-whisper", version="1.2.1", language="zh",
                spans=[dict(start=.2, end=.75, text="机器人在说话", no_speech_prob=.01, avg_logprob=-.3)],
            )), encoding="utf-8")
        return b"", b""

    monkeypatch.setattr(processing_units, "command", command)
    movie = tmp_path / "unsubtitled.mp4"
    subprocess.run([binaries["ffmpeg"], "-v", "error", "-f", "lavfi", "-i", "color=s=80x48:d=2",
                    "-f", "lavfi", "-i", "sine=duration=2", "-c:v", "libx264", "-c:a", "aac", str(movie)],
                   check=True)
    with TestClient(create_app(work_dir=tmp_path)) as client:
        project, asset = ingest(client, movie, tmp_path)
        wait_index(client, asset, 25)
        assert len(calls) == 2
        result = body(client.post("/v1/search", json=dict(
            scope=dict(project_id=project), query_mode="LOCAL", text="机器人", top_k=1,
        )))
        hit, = result["hits"]
        assert hit["route_ranks"][0]["route"] == "fts5"
        transcript = body(client.get("/v1/evidence/" + hit["evidence_ids"][0]))
        assert transcript["range"] == {"start_us": 200_000, "end_us": 750_000}
        notes = body(client.get(f"/v1/assets/{asset['asset_id']}/notes"))
        artifact = body(client.get(f"/v1/artifacts/{notes['artifact_id']}"))
        report = body(client.get(artifact["url"]))
        assert report["chapters"][0]["claims"][0]["text"] == "机器人在说话"
