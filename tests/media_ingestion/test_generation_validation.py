"""A broken evidence reference in a BUILDING run must never change its readable predecessor."""

import asyncio
import os
import shutil
import subprocess
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from dovideo.infrastructure.media.processing_units import ProcessingUnits
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.processing_tables import records
from test_processing_recovery import worker_at


@pytest.mark.real_media
def test_readback_rejects_missing_artifacts_and_wrong_revision(tmp_path, monkeypatch):
    exe = shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not exe:
        pytest.skip("FFmpeg required")
    monkeypatch.delenv("DOVIDEO_PADDLE_OCR_DIR", raising=False)
    path = tmp_path / "small.mp4"
    subprocess.run(
        [exe, "-v", "error", "-f", "lavfi", "-i", "color=s=64x48:r=8:d=1", "-c:v", "libx264", str(path)],
        check=True,
    )
    worker = worker_at(tmp_path)
    cat = worker.catalog
    scope = str(uuid4())
    project = cat.sources.create_project(scope, "publication")["project_id"]
    row = cat.sources.create_upload(
        scope, project, dict(filename=path.name, size_bytes=path.stat().st_size, content_type="video/mp4")
    )
    checksum = digest_file(path)
    cat.sources.receive(row, path, checksum, row.size)
    cat.sources.complete(
        scope,
        row.upload,
        dict(idempotency_key="source-initial", parts=[dict(part_number=1, etag=checksum)]),
    )

    def build(key):
        worker.enqueue(scope, row.id, dict(idempotency_key=key, requested_capabilities=["ocr"]))
        return cat.claim()

    worker.perform(build("intact"))
    head, original = cat.search_scope(scope, project)
    validate = ProcessingUnits.validate_artifacts
    for field, value in (("media_revision_id", str(uuid4())), ("artifact_ids", [str(uuid4())])):
        run = build("broken-" + field)

        def corrupt(unit):
            with cat.write() as db:
                item = db.execute(
                    select(records).where(records.c.run == run.id, records.c.kind == "EVIDENCE").limit(1)
                ).one()
                evidence = dict(item.body["evidence"], **{field: value})
                db.execute(
                    update(records)
                    .where(records.c.id == item.id)
                    .values(body=dict(item.body, evidence=evidence))
                )
            validate(unit)

        with monkeypatch.context() as patch:
            patch.setattr(ProcessingUnits, "validate_artifacts", corrupt)
            worker.stopping = asyncio.Event()
            asyncio.run(worker.execute(run))
        assert cat.get(scope, run.id)["status"] == "FAILED"
        assert cat.search_scope(scope, project) == (head, original)
