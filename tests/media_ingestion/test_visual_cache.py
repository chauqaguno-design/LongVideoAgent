"""Rebuild a real video across three generations with an offline visual protocol fixture."""

import json
import os
import shutil
import subprocess
from uuid import uuid4

import pytest

from dovideo.infrastructure.media import (
    processing_units as units,
    visual_observations as visual,
)
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.workspace_budget_table import workspace_budgets
from dovideo.infrastructure.providers.config import ProviderConfig
from test_processing_recovery import worker_at


@pytest.mark.real_media
def test_visual_cache_rebinds_frames_and_invalidates_only_affected_stage(tmp_path, monkeypatch):
    exe = shutil.which("ffmpeg", path=os.environ.get("DOVIDEO_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    worker = worker_at(tmp_path)
    catalog = worker.catalog
    with catalog.sessions() as db:
        workspace_budgets.create(db.connection())
        db.commit()
    scope = str(uuid4())
    project = catalog.sources.create_project(scope, "cache")["project_id"]
    config = ProviderConfig("https://offline.example/v1", "test-flash")
    monkeypatch.setattr(
        units,
        "visual_deployment",
        lambda env: visual.VisualDeployment(config, "test-v1", True),
    )
    for k in (
        "DOVIDEO_MEDIA_VISUAL_MAX_CNY_MICROUNITS",
        "DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS",
    ):
        monkeypatch.setenv(k, "100000")
    monkeypatch.setenv("DOVIDEO_FLASH_PRICE_VERSION", "test")
    for k in (
        "DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION",
        "DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION",
    ):
        monkeypatch.setenv(k, "1000")
    calls = []

    def post(_, request):
        ids = [
            item["text"].split()[0]
            for item in json.loads(request)["messages"][1]["content"]
            if "text" in item
        ]
        calls.append(ids)
        claims = {
            "observations": [
                dict(
                    text="公开测试观察",
                    frames=ids,
                    claim_type="OBSERVED",
                    temporal=True,
                )
            ]
        }
        return dict(
            model="test-v1",
            usage=dict(prompt_tokens=40, completion_tokens=10, total_tokens=50),
            choices=[{"message": {"content": json.dumps(claims)}}],
        )

    monkeypatch.setattr(visual, "post", post)

    def upload(color):
        path = tmp_path / f"{color}.mp4"
        subprocess.run(
            [
                exe,
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x48:r=8:d=2",
                "-f",
                "lavfi",
                "-i",
                "sine=duration=2",
                "-c:v",
                "libx264",
                "-c:a",
                "aac",
                str(path),
            ],
            check=True,
        )
        row = catalog.sources.create_upload(
            scope,
            project,
            dict(
                filename=path.name,
                size_bytes=path.stat().st_size,
                content_type="video/mp4",
            ),
        )
        checksum = digest_file(path)
        catalog.sources.receive(row, path, checksum, row.size)
        catalog.sources.complete(
            scope,
            row.upload,
            dict(idempotency_key="upload", parts=[dict(part_number=1, etag=checksum)]),
        )
        return row

    def build(asset, key, expected_calls):
        worker.enqueue(
            scope,
            asset.id,
            dict(
                idempotency_key=key,
                requested_capabilities=["asr", "visual_observation"],
            ),
        )
        run = catalog.claim()
        worker.perform(run)
        assert catalog.visual_usage(scope, run.id)["model_calls"] == expected_calls
        artifact = next(r for r in catalog.entries(run, "ARTIFACT") if r.unit == "visual-window:0")
        report = json.loads(catalog.derived_path(scope, artifact.body["file"]).read_text(encoding="utf8"))
        frames = {r.id for r in catalog.entries(run, "ARTIFACT") if "frame" in r.body}
        evidence = next(
            r.body for r in catalog.entries(run, "OBSERVATION") if r.body["modality"] == "VISUAL"
        )
        assert set(evidence["artifact_ids"]) == frames | {artifact.id}
        return report, artifact

    first = upload("red")
    original, _ = build(first, "initial", 1)
    cache_dir = catalog.root / first.id / "cache"
    cache_times = {f: f.stat().st_mtime_ns for f in cache_dir.glob("*.json")}
    if os.environ.get("DOVIDEO_WHISPER_DIR"):
        assert cache_times
    replay, artifact = build(first, "unchanged", 0)
    assert (
        original["response"] == replay["response"]
        and replay["producing_run_id"] == original["producing_run_id"]
    )
    assert not set(replay["input_frame_ids"]) & set(replay["replay_frame_ids"].values())
    assert catalog.get_record(str(uuid4()), artifact.id, "ARTIFACT") is None
    monkeypatch.setattr(units, "PROMPT_VERSION", "new-observation-protocol")
    monkeypatch.setattr(visual, "PROMPT_VERSION", "new-observation-protocol")
    changed, _ = build(first, "prompt-change", 1)
    assert changed["producing_run_id"] != original["producing_run_id"]
    assert cache_times == {f: f.stat().st_mtime_ns for f in cache_dir.glob("*.json")}
    second = upload("blue")
    new_source, _ = build(second, "new-bytes", 1)
    assert new_source["producing_run_id"] != changed["producing_run_id"]
    assert len(calls) == 3
