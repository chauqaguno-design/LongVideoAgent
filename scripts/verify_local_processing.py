"""Run the actual processing worker against a user-selected local video and emit an evidence summary."""

import argparse
import json
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from sqlalchemy import create_engine

from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.processing_worker import ProcessingWorker
from dovideo.infrastructure.persistence.outbox_tables import outbox
from dovideo.infrastructure.persistence.processing_tables import records, runs
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.sqlalchemy import Base
from dovideo.infrastructure.search_encoder import local_encoder


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    source = args.source.resolve()
    engine = create_engine(f"sqlite:///{(args.output/'results.sqlite3').resolve().as_posix()}")
    Base.metadata.create_all(engine, tables=[projects, sources, runs, records, outbox])
    store = EditSources(engine, args.output)
    worker = ProcessingWorker(store)
    encoder = local_encoder(store.root)
    if encoder:
        worker.catalog.prepare_search_rows = encoder.prepare
    store.enqueue_completed = worker.register_upload
    scope = str(uuid4())
    project = store.create_project(scope, "媒体处理验收")["project_id"]
    sha = digest_file(source)
    row = store.create_upload(
        scope, project, dict(filename=source.name, size_bytes=source.stat().st_size, content_type="video/mp4")
    )
    store.receive(row, source, sha, row.size)
    store.complete(
        scope, row.upload, dict(parts=[dict(part_number=1, etag=sha)], idempotency_key="test-source")
    )
    run = worker.catalog.claim()
    started = perf_counter()
    worker.perform(run)
    result = worker.catalog.get(scope, run.id)
    obs = [r.body for r in worker.catalog.entries(run, "OBSERVATION")]
    frames = [r.body["frame"] for r in worker.catalog.entries(run, "ARTIFACT") if "frame" in r.body]
    generation, hits = worker.catalog.search_scope(scope, project, [row.revision])
    coverage = [r.body for r in worker.catalog.entries(run, "COVERAGE")]
    report = dict(
        source=source.name,
        digest=sha,
        run=result,
        asset=store.asset(scope, row.id),
        frames=frames,
        elapsed_seconds=round(perf_counter() - started, 3),
        coverage=coverage,
        observations=obs,
        generation=generation,
        evidence_count=len(hits),
    )
    (args.output / "verification.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(
        result["status"],
        len(frames),
        "frames",
        len(obs),
        "observations",
        [(c["name"], c["state"]) for c in report["asset"]["capabilities"]],
    )
    if result["status"] == "FAILED":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
