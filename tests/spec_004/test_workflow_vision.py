"""Real FFmpeg timed frames + controlled provider protocol. Makes no network call."""

import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess

from PIL import Image
import pytest
from sqlalchemy import create_engine, update
from dovideo.application.workflow_inspections import (
    WaitInspection,
    inspection,
    pending_inspection,
    run_inspection,
)
from dovideo.application.workflow_tools import job_status
from dovideo.application.workflow_shots import distinct_candidates
from dovideo.application.edit_sources import EditSources
from dovideo.application.edit_timeline import uid
from dovideo.application.workflow_state import DEFAULT_BUDGET
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.edit_tables import EDIT_TABLES
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base
from dovideo.infrastructure.persistence.workflow_repository import WorkflowRepository
from dovideo.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES
from dovideo.infrastructure.workflow_vision import review_interval


class ColorSequenceVerifier:
    env = {
        "DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS": "100000",
        "DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION": "10",
        "DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION": "20",
        "DOVIDEO_FLASH_PRICE_VERSION": "offline-fixture",
    }

    def identity(self, *, vision=False):
        assert vision
        return {"model": "fixture", "mode": "json", "fingerprint": "fixture"}

    def request(self, dialogs, identity, tools, deadline):
        images = [
            x["image_url"]["url"].split(",")[1] for x in dialogs[1]["content"] if x["type"] == "image_url"
        ]
        colors = []
        for image in images:
            with Image.open(io.BytesIO(base64.b64decode(image))) as im:
                colors.append(im.convert("RGB").getpixel((im.width // 2, im.height // 2)))
        observed = colors[0][0] > 200 and colors[-1][2] > 200
        value = dict(
            support="SUPPORTED" if observed else "CONFLICTING",
            reason="观察相邻帧中的颜色变化",
            frame_indices=[0, len(images) - 1],
        )
        return {
            "usage": {"prompt_tokens": 100, "completion_tokens": 16},
            "choices": [{"message": {"content": json.dumps(value)}}],
        }


def test_candidate_budget_covers_distinct_source_ranges():
    ranges = [(0, 10), (3, 7), (0, 10), (8, 15), (9, 13)]
    hits = [dict(media_revision_id="film", range=dict(start_us=start, end_us=end)) for start, end in ranges]
    assert list(distinct_candidates(hits)) == [(0, hits[0]), (1, hits[1]), (3, hits[3])]
    assert [position for position, _ in distinct_candidates(hits, 5)] == [0, 1, 3, 4]


@pytest.mark.real_media
def test_ordered_source_frames_and_output_review(tmp_path):
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    movie = tmp_path / "two-colors.mp4"
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=red:s=64x48:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=64x48:d=1",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0[v]",
            "-map",
            "[v]",
            "-c:v",
            "libx264",
            str(movie),
        ],
        check=True,
    )
    engine = create_engine(f"sqlite:///{tmp_path}/vision.db")
    Base.metadata.create_all(engine, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    store = EditSources(engine, tmp_path)
    scope = uid()
    project = store.create_project(scope, "视觉验收")["project_id"]
    repo = WorkflowRepository(EditRepository(store))
    asset = store.create_upload(
        scope, project, dict(filename="colors.mp4", size_bytes=movie.stat().st_size, content_type="video/mp4")
    )
    with repo.edits.write() as db:
        db.execute(
            update(sources)
            .where(sources.c.id == asset.id)
            .values(
                digest="0" * 64,
                receipt="a" * 64,
                probe=dict(
                    duration_us=2_000_000, width=64, height=48, streams=[], source_origin=dict(num=0, den=1)
                ),
            )
        )
    store.source_path = lambda workspace, mid: movie
    run = repo.create(
        scope,
        dict(
            scope=dict(project_id=project),
            intent="INSPECT",
            message="哪段画面从红色变成蓝色？",
            budget=dict(DEFAULT_BUDGET, max_cost_microunits=100000),
            idempotency_key="sample-vision",
        ),
    )
    row = repo.claim(uid())
    revision = asset.revision
    provider = ColorSequenceVerifier()
    result = review_interval(
        repo, provider, row, "source-qa", movie, revision, dict(start_us=0, end_us=2_000_000), "红色变蓝"
    )
    assert result["support"] == "SUPPORTED"
    evidence = repo.document(scope, result["evidence_id"]).body
    assert evidence["media_revision_id"] == revision and evidence["artifact_ids"] == [result["sequence_id"]]
    frames = repo.document(scope, evidence["artifact_ids"][0]).body["frames"]
    assert len(frames) == 3 and frames[0]["pts"] < frames[1]["pts"] < frames[2]["pts"]
    assert (
        review_interval(
            repo,
            provider,
            row,
            "output-qa",
            movie,
            None,
            dict(start_us=1000000, end_us=2_000_000),
            "红色变蓝",
        )["support"]
        == "CONFLICTING"
    )
    assert repo.get(scope, run["run_id"])["budget_usage"]["images"] == 6
    assert repo.document(uid(), result["sequence_id"]) is None
    with pytest.raises(WaitInspection) as queued:
        inspection(row, "queued-visual", revision, dict(start_us=0, end_us=2_000_000), "红色变蓝")
    repo.advance(row, **queued.value.outcome)
    restarted = WorkflowRepository(repo.edits)
    claimant = restarted.claim(uid())
    assert claimant.state["status"] == "WAITING_JOB"
    job = pending_inspection(claimant)
    assert job_status(restarted, None, claimant, job["job_id"])["terminal"] is False
    restarted.advance(claimant, **run_inspection(restarted, provider, None, claimant, job))
    resumed = restarted.claim(uid())
    public = job_status(restarted, None, resumed, job["job_id"])
    assert public["kind"] == "INSPECTION" and public["terminal"] and public["result_artifact_ids"]
    assert inspection(resumed, "queued-visual", revision, job["range"], "红色变蓝")["support"] == "SUPPORTED"
    with pytest.raises(WaitInspection) as queued:
        inspection(resumed, "single-frame", revision, dict(start_us=0, end_us=1), "判断动作")
    restarted.advance(resumed, **queued.value.outcome)
    single = restarted.claim(uid())
    small = pending_inspection(single)
    restarted.advance(single, **run_inspection(restarted, provider, None, single, small))
    resumed = restarted.claim(uid())
    partial = job_status(restarted, None, resumed, small["job_id"])
    assert partial["status"] == "PARTIAL" and partial["terminal"] and not partial["result_artifact_ids"]
    engine.dispose()
