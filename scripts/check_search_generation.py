"""Exercise a two-generation media rebuild on dedicated MySQL and Qdrant test services."""

import argparse
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

from sqlalchemy import create_engine

from dovideo.application.edit_sources import EditSources
from dovideo.application.evidence_search import EvidenceSearch
from dovideo.infrastructure.media.source_artifacts import digest_file
from dovideo.infrastructure.persistence.processing_worker import ProcessingWorker
from dovideo.infrastructure.search_encoder import local_encoder


def run(source, root):
    engine = create_engine(os.environ["VIDEOMIND_BENCH_MYSQL_URL"])
    sources = EditSources(engine, root)
    worker = ProcessingWorker(sources)
    search = EvidenceSearch(sources, worker.catalog, local_encoder(root))
    worker.catalog.prepare_search_rows = search.prepare_rows
    sources.enqueue_completed = worker.register_upload
    ws = str(uuid4())
    pid = sources.create_project(ws, "检索快照验收")["project_id"]
    digest = digest_file(source)
    upload = sources.create_upload(
        ws, pid, dict(filename=source.name, size_bytes=source.stat().st_size, content_type="video/mp4")
    )
    sources.receive(upload, source, digest, upload.size)
    sources.complete(
        ws, upload.upload, dict(parts=[dict(part_number=1, etag=digest)], idempotency_key="probe")
    )

    run1 = worker.catalog.claim()
    worker.perform(run1)
    old_id, rows1 = worker.catalog.search_scope(ws, pid, [upload.revision])
    cue = next(r["claim"] for r in rows1 if r["source"] == "embedded-subtitle")
    query = dict(
        scope=dict(project_id=pid, media_revision_ids=[upload.revision]),
        text=cue,
        query_mode="LOCAL",
        top_k=10,
    )
    pinned = search.plan(ws, query)
    worker.enqueue(
        ws,
        upload.id,
        dict(idempotency_key="rebuild", requested_capabilities=run1.config["requested_capabilities"]),
    )
    run2 = worker.catalog.claim()
    during = search.search(ws, query)
    worker.perform(run2)
    current = search.search(ws, query)
    earlier = search.rank(ws, query, pinned)
    new_id, rows2 = worker.catalog.search_scope(ws, pid, [upload.revision])
    _, historical = worker.catalog.search_scope(ws, pid, [upload.revision], generation_id=old_id)

    def evidence_ids(rows):
        return {r["evidence_id"] for r in rows}

    def verify(result, expected_id, rows):
        assert result["index_generation_id"] == expected_id
        assert {eid for h in result["hits"] for eid in h["evidence_ids"]} <= evidence_ids(rows)
        routes = {rank["route"] for hit in result["hits"] for rank in hit["route_ranks"]}
        assert {"text_qdrant", "vision_qdrant"} <= routes, (routes, result["warnings"])
        return sorted(routes)

    old_routes = verify(during, old_id, rows1)
    verify(earlier, old_id, rows1)
    new_routes = verify(current, new_id, rows2)
    cold_search = EvidenceSearch(sources, worker.catalog, local_encoder(root / "separate-node"))
    cold_routes = verify(cold_search.search(ws, query), new_id, rows2)
    cold_search.close()
    assert old_id != new_id and evidence_ids(rows1).isdisjoint(evidence_ids(rows2))
    assert evidence_ids(rows1) == evidence_ids(historical), "Historical source IDs changed"
    assert rows1 and rows2
    search.close()
    engine.dispose()
    return dict(
        source_sha256=digest,
        generations=[old_id, new_id],
        source_observations=[len(rows1), len(rows2)],
        building_reads_old=True,
        pinned_reader_after_publish_reads_old=True,
        active_reads_new=True,
        historical_snapshot_matches=True,
        intersection_evidence_ids=0,
        retrieval_routes_before=old_routes,
        retrieval_routes_after=new_routes,
        cold_encoder_cache_routes=cold_routes,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="A local MP4 with an embedded subtitle track")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with TemporaryDirectory() as tmp:
        report = run(args.source.resolve(), Path(tmp))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print("Old reader, active reader, and both Qdrant routes verified across a MySQL publication.")
