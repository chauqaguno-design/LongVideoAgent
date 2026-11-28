"""Compare independent QA on one real exported revision with controlled faults in its MP4 bytes."""

import json
import shutil
import subprocess
from base64 import b64decode
from io import BytesIO

import pytest
from PIL import Image
from sqlalchemy import create_engine, insert, select

from cutsieve.application.agent_tools import AgentTools, Frames
from cutsieve.application.edit_sources import EditSources
from cutsieve.application.edit_timeline import EditError, uid
from cutsieve.infrastructure.edit_captions import Cue, write_srt
from cutsieve.infrastructure.edit_media import inspect_output
from cutsieve.infrastructure.media.source_artifacts import digest_file
from cutsieve.infrastructure.persistence.edit_repository import EditRepository
from cutsieve.infrastructure.persistence.edit_tables import EDIT_TABLES, artifacts, jobs
from cutsieve.infrastructure.persistence.render_worker import RenderWorker, media_binaries
from cutsieve.infrastructure.persistence.source_tables import projects, sources
from cutsieve.infrastructure.persistence.sqlalchemy import Base
from cutsieve.infrastructure.persistence.workflow_repository import WorkflowRepository
from cutsieve.infrastructure.persistence.workflow_tables import WORKFLOW_TABLES


def render_control(root, tools, edits, scope, project, cues):
    """Register and render a timeline with known red-to-blue frames and an independently authored text cue."""
    source = root / "authored.mp4"
    ff = [tools["ffmpeg"], "-nostdin", "-v", "error"]
    subprocess.run(
        [
            *ff,
            "-f",
            "lavfi",
            "-i",
            "color=red:s=96x64:r=12:d=1",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=96x64:r=12:d=1",
            "-filter_complex",
            "concat=n=2:v=1:a=0",
            "-c:v",
            "libx264",
            str(source),
        ],
        check=True,
    )
    store = edits.sources
    row = store.create_upload(
        scope,
        project,
        dict(filename=source.name, size_bytes=source.stat().st_size, content_type="video/mp4"),
    )
    sha = digest_file(source)
    store.receive(row, source, sha, row.size)
    store.complete(scope, row.upload, dict(parts=[dict(part_number=1, etag=sha)], idempotency_key="upload"))
    profile = dict(width=96, height=64, fps_num=12, fps_den=1, audio_sample_rate=48000)
    edit = edits.create(
        scope, dict(project_id=project, title="成片复核", output_profile=profile, idempotency_key="edit")
    )
    track = uid()
    edits.resolve_track = lambda *args: cues
    clip = dict(
        media_revision_id=row.revision,
        source_start_us=0,
        source_end_us=2_000_000,
        audio_policy="keep",
        evidence_ids=[],
        caption_track_id=track,
    )
    rev = edits.patch(
        scope,
        edit["edit_project_id"],
        dict(
            expected_revision=0,
            reason="红色变蓝色",
            idempotency_key="cut",
            operations=[
                dict(op="append_clip", clip=clip),
                dict(op="set_caption_policy", caption_policy="preserve_source"),
            ],
        ),
    )
    job = edits.enqueue(
        scope, dict(revision_id=rev["revision_id"], profile="EXPORT", idempotency_key="render")
    )
    worker = RenderWorker(edits, None)
    worker.produce(worker.claim())
    with edits.sessions() as db:
        result = db.scalar(select(jobs.c.result).where(jobs.c.id == job["render_job_id"]))

    def path(key):
        return worker.materialize(worker.artifact_record(scope, result[key]))

    manifest = json.loads(path("compiled_manifest_artifact_id").read_text(encoding="utf8"))
    report = json.loads(path("validation_report_id").read_text(encoding="utf8"))
    assert report["media"]["structure"]["timeline_revision_id"] == rev["revision_id"]
    return worker, manifest, result["artifact_id"], path("artifact_id")


def inspect_frames(repo, worker, scope, project, artifact_id, case):
    repo.create(scope, dict(scope=dict(project_id=project), message="画面从红色变成蓝色", idempotency_key=case))
    row = repo.claim(uid())
    output = AgentTools(repo, None, worker).inspect_frames(
        row, Frames(artifact_id=artifact_id, timestamps_s=[.25, 1.25])
    )
    with repo.change(row) as (db, current):
        repo.complete(db, current, current.checkpoint.result("Checked frames."))
    return output


@pytest.mark.real_media
def test_playable_wrong_event_and_shifted_caption_have_independent_results(tmp_path):
    tools = media_binaries()
    if not all(shutil.which(str(p)) for p in tools.values()):
        pytest.skip("FFmpeg and ffprobe required")
    db = create_engine("sqlite:///" + (tmp_path / "qa.db").as_posix())
    Base.metadata.create_all(db, tables=[projects, sources, *EDIT_TABLES, *WORKFLOW_TABLES])
    edits = EditRepository(EditSources(db, tmp_path))
    repo = WorkflowRepository(edits)
    scope = uid()
    project = edits.sources.create_project(scope, "双重成片验证")["project_id"]
    cues = [Cue(250_000, 750_000, "RED")]
    worker, manifest, control_id, control = render_control(tmp_path, tools, edits, scope, project, cues)
    revision = manifest["revision_id"]
    case_dir = worker.root / "qa-fixtures"
    case_dir.mkdir()
    ff = [tools["ffmpeg"], "-nostdin", "-v", "error", "-i", str(control)]
    wrong_event = case_dir / "wrong-event.mp4"
    subprocess.run(
        [*ff, "-map", "0", "-vf", "reverse", "-c", "copy", "-c:v", "libx264", str(wrong_event)], check=True
    )
    missing_audio = case_dir / "missing-audio.mp4"
    subprocess.run([*ff, "-map", "0:v", "-map", "0:s", "-c", "copy", str(missing_audio)], check=True)
    late_text = tmp_path / "late.srt"
    write_srt(late_text, [Cue(1_250_000, 1_750_000, "RED")])
    shifted = case_dir / "shifted-subtitle.mp4"
    subprocess.run(
        [
            *ff,
            "-i",
            str(late_text),
            "-map",
            "0:v",
            "-map",
            "0:a",
            "-map",
            "1:s",
            "-c",
            "copy",
            "-c:s",
            "mov_text",
            str(shifted),
        ],
        check=True,
    )
    for label, path, structural, semantic in (
        ("control", control, "PASSED", [0, 2]),
        ("wrong-event", wrong_event, "PASSED", [2, 0]),
        ("shifted-subtitle", shifted, "FAILED", [0, 2]),
        ("missing-audio", missing_audio, "FAILED", [0, 2]),
    ):
        aid = control_id
        if path != control:
            # Insert the defective bytes as a QA-only artifact so the two independent
            # reviews can inspect it even when the normal publisher would reject it.
            aid = uid()
            with edits.write() as session:
                session.execute(
                    insert(artifacts).values(
                        id=aid,
                        workspace_id=scope,
                        revision_id=revision,
                        kind="EXPORT",
                        digest=digest_file(path),
                        size=path.stat().st_size,
                        relative_path=path.relative_to(worker.root).as_posix(),
                    )
                )
        check = inspect_output(path, manifest, soft_cues=cues, **tools)
        assert check["status"] == structural
        if label == "shifted-subtitle":
            assert "subtitle times" in check["issue"]
        if label == "missing-audio":
            assert "audio stream" in check["issue"]
        outcome = inspect_frames(repo, worker, scope, project, aid, label)
        assert revision == check["timeline_revision_id"] == outcome.body["revision_id"]
        assert check["output_sha256"] == digest_file(path)
        assert aid == outcome.body["artifact_id"]
        assert [frame["timestamp_s"] for frame in outcome.body["frames"]] == [.25, 1.25]
        colors = []
        for part in outcome.images:
            pixels = b64decode(part["image_url"]["url"].split(",", 1)[1])
            with Image.open(BytesIO(pixels)) as image:
                rgb = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
                colors.append(max(range(3), key=rgb.__getitem__))
        assert colors == semantic
    other = edits.sources.create_project(scope, "Other project")["project_id"]
    with pytest.raises(EditError) as stale:
        inspect_frames(repo, worker, scope, other, control_id, "wrong-project")
    assert stale.value.status == 404
    bad_id = uid()
    original = worker.artifact_record(scope, control_id)
    with edits.write() as session:
        session.execute(insert(artifacts).values(dict(original._mapping, id=bad_id, kind="VALIDATION_REPORT")))
    with pytest.raises(EditError) as wrong_kind:
        inspect_frames(repo, worker, scope, project, bad_id, "wrong-kind")
    assert wrong_kind.value.code == "NOT_A_VIDEO"
    db.dispose()
