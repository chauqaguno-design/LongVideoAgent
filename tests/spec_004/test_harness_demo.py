"""The review can disagree with a first-pass detector. Media and effects are real."""

from base64 import b64decode
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from uuid import uuid4
import json
import os
import subprocess
import sys
import time

from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select, update
import pytest

from dovideo.application.workflow_identity import context_estimate
from dovideo.application.workflow_state import DEFAULT_BUDGET
from dovideo.infrastructure.persistence.render_worker import media_binaries
from dovideo.infrastructure.persistence.workflow_tables import runs
from dovideo.infrastructure.persistence.workflow_worker import WorkflowWorker
from dovideo.presentation.api.app import create_app
from test_conversation_runs import login, post, upload


class ControlledCoverage:
    env = dict(
        DOVIDEO_WORKFLOW_LIVE_MODEL="1",
        DOVIDEO_WORKSPACE_CNY_MAX_COST_MICROUNITS="1000000",
        DOVIDEO_FLASH_CNY_INPUT_MICROS_PER_MILLION="100",
        DOVIDEO_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="100",
        DOVIDEO_FLASH_PRICE_VERSION="controlled-demo-v1",
    )

    def identity(self, *, vision=False):
        return dict(model="controlled-color-v1", fingerprint="synthetic-frames", mode="native")

    def request(self, messages, identity, tools, deadline):
        system = messages[0]["content"]
        content = messages[1]["content"]
        if system.startswith("Extract editing constraints"):
            alternate = "Alternative" in content
            plan = dict(
                query="字幕：「Alternative」" if alternate else "字幕：「Marker」",
                must_include=["蓝色画面"] if alternate else [],
                must_exclude=[],
                order_policy="SOURCE_ORDER",
                audio="KEEP_SOURCE",
            )
            if not alternate:
                plan["duration_target_us"] = 10_000_000
            result = dict(type="FINAL", plan=plan)
        else:
            colors = []
            for part in content[1:]:
                with Image.open(BytesIO(b64decode(part["image_url"]["url"].split(",", 1)[1]))) as image:
                    colors.append(image.convert("RGB").getpixel((image.width // 2, image.height // 2)))
            red = [i for i, (r, g, b) in enumerate(colors) if r > 200 and g < 40 and b < 40]
            # The source detector accepts the marker candidates. Independent output inspection catches
            # the false positive from the first pass and cites its two render-frame observations.
            conflict = "visible fact that contradicts" in system and len(red) >= 2
            result = dict(
                support="CONFLICTING" if conflict else "SUPPORTED",
                reason=(
                    "两个成片采样时刻均显示红色，与目标蓝色冲突。"
                    if conflict
                    else "采样中观察到了目标彩色镜头。"
                ),
                frame_indices=red if conflict else [0, len(colors) - 1],
            )
        wire = json.dumps(result, ensure_ascii=False)
        tokens = context_estimate(messages, tools)["input_tokens"]
        return dict(
            choices=[dict(message=dict(content=wire))],
            usage=dict(prompt_tokens=tokens // 4, completion_tokens=len(wire) // 4),
        )


@pytest.fixture
def color_movie(tmp_path):
    binaries = media_binaries()
    try:
        subprocess.run([binaries["ffmpeg"], "-version"], capture_output=True, check=True)
    except OSError:
        pytest.skip("FFmpeg required")
    captions = tmp_path / "markers.srt"
    captions.write_text(
        "1\n00:00:00,000 --> 00:00:04,000\nMarker\n\n"
        "2\n00:00:04,000 --> 00:00:10,000\nMarker\n\n"
        "3\n00:00:10,000 --> 00:00:16,000\nAlternative\n\n"
        "4\n00:00:16,000 --> 00:00:22,000\nAlternative\n",
        encoding="utf8",
    )
    movie = tmp_path / "known-colors.mp4"
    command = [binaries["ffmpeg"], "-nostdin", "-v", "error"]
    for color, length in (("yellow", 4), ("green", 6), ("red", 6), ("blue", 6)):
        command.extend(["-f", "lavfi", "-i", f"color={color}:s=96x64:r=10:d={length}"])
    command += [
        "-i",
        str(captions),
        "-filter_complex",
        "concat=n=4:v=1:a=0[out]",
        "-map",
        "[out]",
        "-map",
        "4:s",
        "-c:v",
        "libx264",
        "-c:s",
        "mov_text",
        str(movie),
    ]
    subprocess.run(command, check=True)
    return movie


def lost_worker(directory, run_id):
    worker = create_app(work_dir=Path(directory)).state.workflow_worker
    apply = worker.repo.edits.patch

    def commit_and_exit(*args, **kwargs):
        apply(*args, **kwargs)
        os._exit(77)

    worker.repo.edits.patch = commit_and_exit
    claim = worker.repo.claim(worker.worker_id)
    assert claim.id == run_id and claim.state["phase"] == "COMMIT"
    worker.run_stage(claim)
    raise AssertionError("expected process loss after commit")


def drive(app, run_id, directory, *, lose_worker=False):
    worker = app.state.workflow_worker
    repo = worker.repo
    started = time.monotonic()
    while time.monotonic() < started + 80:
        with repo.sessions() as db:
            row = db.execute(select(runs).where(runs.c.id == run_id)).one()
        if row.status in ("SUCCEEDED", "FAILED", "AWAITING_INPUT"):
            return worker, row, round((time.monotonic() - started) * 1000)
        if lose_worker and row.state["phase"] == "COMMIT":
            child = subprocess.run(
                [sys.executable, str(Path(__file__).resolve()), str(directory), run_id], timeout=25
            )
            assert child.returncode == 77
            with repo.edits.write() as db:
                previous_fence = db.scalar(select(runs.c.fence).where(runs.c.id == run_id))
                db.execute(update(runs).where(runs.c.id == run_id).values(lease=1))
            app.state.workflow_worker = replacement = WorkflowWorker(repo, app, worker.sign)
            replacement.planner = worker.planner
            worker = replacement
            takeover = repo.claim(worker.worker_id)
            assert takeover.fence > previous_fence and takeover.checkpoint.attempt.plan_generation == 0
            worker.run_stage(takeover)
            lose_worker = False
        elif claim := repo.claim(worker.worker_id):
            worker.run_stage(claim)
        else:
            time.sleep(0.12)
    pytest.fail(f"{run_id} did not settle")


@pytest.mark.real_media
@pytest.mark.parametrize("repair_limit", [0, 2], ids=["no-repair", "bounded-repair"])
def test_grounded_edit_recovery_repair_and_export(tmp_path, monkeypatch, color_movie, repair_limit):
    monkeypatch.setenv("DOVIDEO_WORKFLOW_REPAIR_LIMIT", str(repair_limit))

    async def manual_start(self):
        pass

    monkeypatch.setattr(WorkflowWorker, "start", manual_start)
    directory = tmp_path / "app"
    app = create_app(work_dir=directory)
    with TestClient(app) as client:
        login(client, "harness_demo")
        app.state.workflow_worker.planner = ControlledCoverage()
        project = post(client, "/v1/projects", title="可复现镜头修复")["project_id"]
        media = upload(client, project, color_movie)
        limits = dict(DEFAULT_BUDGET, max_cost_microunits=500_000, max_model_calls=24, max_images=36)

        def say(message, parent=None):
            return post(
                client,
                f"/v1/projects/{project}/conversation-runs",
                message=message,
                parent_run_id=parent,
                media_revision_id=media,
                budget=limits,
                idempotency_key=str(uuid4()),
            )["run_id"]

        first = say("找到说「Marker」时出现的画面，确认后剪出来，总时长10秒")
        _, initial, _ = drive(app, first, directory)
        assert initial.status == "SUCCEEDED", initial.state
        first_output = client.get(f"/v1/workflow-runs/{first}/result").json()
        assert len(first_output["shot_plan"]["shots"]) == 2
        original_id = first_output["revision_id"]
        edit = first_output["edit_project_id"]
        view = f"/v1/edit-projects/{edit}/revisions/"
        original = client.get(view + original_id).json()["clips"]
        second = say("把第二段换成说「Alternative」时的蓝色画面，其他保持不变，导出 MKV", first)
        worker, finished, elapsed = drive(app, second, directory, lose_worker=True)
        expected = "SUCCEEDED" if repair_limit else "AWAITING_INPUT"
        assert finished.status == expected, finished.state
        result_path = f"/v1/workflow-runs/{second}/result"
        result = client.get(result_path).json()
        output = client.get(view + result["revision_id"]).json()
        assert len(output["clips"]) == 2
        assert output["total_duration_us"] == 10_000_000
        assert output["clips"][0] == original[0]
        assert output["clips"][1]["clip_id"] == original[1]["clip_id"]
        assert result["shot_plan"]["target_clip_ids"] == [original[1]["clip_id"]]
        assert result["shot_plan"]["preserved_clip_ids"] == [original[0]["clip_id"]]
        report = result["review_report"]
        harness = client.get(f"/v1/workflow-runs/{second}/harness").json()
        log = worker.repo.page(finished.workspace_id, second, None, 500)["items"]
        first_log = worker.repo.page(initial.workspace_id, first, None, 500)["items"]
        loaded = {e["payload"]["skill"]["id"] for e in [*first_log, *log]
                  if e["payload"].get("activity") == "skill.loaded"}
        assert loaded == {"evidence-search", "evidence-inspection", "grounded-editing",
                          "timeline-refinement", "output-review"}
        assert any(e["payload"].get("receipt_replayed") for e in log)
        payload = None
        if repair_limit:
            assert output["revision_no"] == 3 and result["repair_count"] == harness["plan_generation"] == 1
            assert output["clips"][1]["source_start_us"] == 16_000_000
            assert (
                report["output_kind"] == "EXPORT" and report["semantic_checks"][0]["status"] == "NOT_CHECKED"
            )
            assert report["structural_checks"][0]["status"] == "PASSED"
            preview_review = result["preview_review"]
            assert preview_review["semantic_checks"][0]["status"] == "PASSED"
            assert preview_review["output_artifact_id"] == result["preview_artifact_id"]
            (old,) = result["lineage"]
            archived = worker.repo.document(finished.workspace_id, old["review_report_id"]).body
            assert (
                archived["semantic_checks"][0]["status"] == "FAILED"
                and archived["issues"][0]["repairability"] == "AUTO"
            )
            assert archived["output_sha256"] != report["output_sha256"]
            artifact = client.get("/v1/artifacts/" + result["export_artifact_id"]).json()
            assert (
                artifact["mime_type"] == "video/x-matroska"
                and artifact["timeline_revision_id"] == output["revision_id"]
            )
            payload = client.get(artifact["url"]).content
            assert sha256(payload).hexdigest() == report["output_sha256"]
        else:
            assert output["revision_no"] == 2 and result["repair_count"] == 0
            assert output["clips"][1]["source_start_us"] == 10_000_000
            assert report["semantic_checks"][0]["status"] == "FAILED" and "export_artifact_id" not in result
        if destination := os.getenv("DOVIDEO_HARNESS_DEMO_OUT"):
            folder = Path(destination)
            folder.mkdir(parents=True, exist_ok=True)
            if payload:
                (folder / "repaired.mkv").write_bytes(payload)
            measurements = dict(
                repair_limit=repair_limit,
                status=finished.status,
                repair_count=result["repair_count"],
                source_sha256=sha256(color_movie.read_bytes()).hexdigest(),
                result=result,
                model_identity=ControlledCoverage().identity(),
                budget_usage=finished.state["budget_usage"],
                elapsed_ms=elapsed,
                wall_clock_kind="local_test_single_run",
                event_log=log,
                duplicate_timeline_patches=0,
                preserved_clips=1,
                mandatory_proof_present=bool(output["clips"][1]["evidence_ids"]),
                controlled_adapter=True,
            )
            (folder / f"repair-{repair_limit}.json").write_text(
                json.dumps(measurements, ensure_ascii=False, indent=2), "utf8"
            )


if __name__ == "__main__":
    lost_worker(*sys.argv[1:])
