"""Reclaim derived search data of expired source tombstones without deleting user screenshots."""

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re

from fastapi import HTTPException
from sqlalchemy import delete, inspect, select

from .persistence.edit_repository import EditRepository
from .persistence.processing_tables import records, runs as processing
from .persistence.search_tables import artifacts, runs
from .persistence.source_tables import projects, sources


@contextmanager
def scoped_write(store, workspace, media_ids=()):
    """Serialize short publication/GC writes. Model inference runs outside this lock."""
    with EditRepository(store).write() as db:
        db.execute(
            select(projects.c.id)
            .where(projects.c.workspace == workspace)
            .order_by(projects.c.id)
            .with_for_update()
        ).all()
        if media_ids:
            ids = set(media_ids)
            rows = db.execute(
                select(sources.c.revision, sources.c.probe)
                .where(sources.c.workspace == workspace, sources.c.revision.in_(ids))
                .with_for_update()
            ).all()
            if {r.revision for r in rows if not (r.probe or {}).get("deleted")} != ids:
                raise HTTPException(404, "素材版本不可访问")
        yield db


def cleanup_search(store, *, cutoff, apply=False, index=None):
    from .vector.evidence_index import EvidenceIndex

    with store.sessions() as db:
        tables = set(inspect(db.get_bind()).get_table_names())
        if artifacts.name not in tables or runs.name not in tables:
            return []
        workspaces = set(db.scalars(select(sources.c.workspace).distinct()))
    owned = False
    if index is None:
        path = os.getenv("VIDEOMIND_SEARCH_QDRANT_PATH")
        url = os.getenv("VIDEOMIND_SEARCH_QDRANT_URL")
        if url or path and Path(path).is_dir():
            index = EvidenceIndex(url=url, path=path)
            owned = True
    removed = []
    try:
        for ws in sorted(workspaces):
            with scoped_write(store, ws) as db:
                rows = db.execute(select(sources).where(sources.c.workspace == ws).with_for_update()).all()
                expired = {
                    r.revision: r
                    for r in rows
                    if (r.probe or {}).get("deleted") and (r.probe or {}).get("deleted_at", 0) <= cutoff
                }
                if not expired:
                    continue
                scoped = artifacts.c.workspace == ws
                all_artifacts = db.execute(select(artifacts).where(scoped)).all()
                doomed = {
                    a.id
                    for a in all_artifacts
                    if a.kind in {"QUERY_RESULT", "QUERY_EVIDENCE"} and expired.keys() & set(a.media_ids)
                }
                keep = {a.digest for a in all_artifacts if a.id not in doomed}
                for a in sorted(doomed):
                    removed.append("query-artifact/" + a)
                candidates = {a.digest for a in all_artifacts if a.id in doomed}
                # Handles a different node's stale cache and an object staged before a failed SQL commit.
                folder = store.root / "query-files" / ws
                if folder.is_dir():
                    candidates.update(
                        f.name
                        for f in folder.iterdir()
                        if re.fullmatch("[0-9a-f]{64}", f.name)
                        and not f.is_symlink()
                        and f.is_file()
                        and f.stat().st_mtime < cutoff
                    )
                if store.objects:
                    prefix = f"videomind/query-files/{ws}/"
                    candidates.update(
                        s.object_name.removeprefix(prefix)
                        for s in store.objects.client.list_objects(
                            store.objects.bucket, prefix=prefix, recursive=True
                        )
                        if s.last_modified.timestamp() < cutoff
                        and re.fullmatch("[0-9a-f]{64}", s.object_name.removeprefix(prefix))
                    )
                for digest in candidates - keep:
                    path = store.root / "query-files" / ws / digest
                    key = f"videomind/query-files/{ws}/{digest}"
                    removed.append(key)
                    if apply:
                        if store.objects:
                            store.objects.client.remove_object(store.objects.bucket, key)
                        path.unlink(missing_ok=True)
                scan_ids = [
                    s.run_id
                    for s in db.execute(select(runs).where(runs.c.workspace == ws))
                    if any(r["media_revision_id"] in expired for r in s.snapshot["requested"])
                ]
                removed.extend("query-scan/" + rid for rid in scan_ids)
                if apply:
                    db.execute(delete(artifacts).where(scoped, artifacts.c.id.in_(doomed)))
                    db.execute(delete(runs).where(runs.c.workspace == ws, runs.c.run_id.in_(scan_ids)))
                if {records.name, processing.name} <= tables:
                    removed += prune_projections(store, db, ws, expired, index, cutoff, apply)
        return sorted(removed)
    finally:
        if owned:
            index.close()


def prune_projections(store, db, ws, expired, index, cutoff, apply):
    from .search_encoder import SearchEncoder

    cache = SearchEncoder("", store.root / "embeddings")
    all_runs = db.execute(
        select(processing).where(processing.c.workspace == ws, processing.c.kind == "INDEX")
    ).all()
    dead = [r for r in all_runs if r.revision in expired]
    live = [r for r in all_runs if r.revision not in expired]
    removed = []
    for mid, source in expired.items():
        components = [r.id for r in dead if r.revision == mid]
        if index and components:
            removed += index.prune(ws, source.project, mid, components, apply)

    def remove(path):
        if path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(store.root.resolve()):
            removed.append(path.relative_to(store.root).as_posix())
            if apply:
                path.unlink(missing_ok=True)

    for r in dead:
        remove(cache.generation_path(ws, r.id))
    # An active builder may be preparing vectors before its final descriptor exists.
    if any(r.status in {"QUEUED", "RUNNING"} for r in live):
        return removed
    retain = set()
    for run in live:
        manifest = cache.generation_path(ws, run.id)
        spaces = db.scalar(
            select(records.c.body).where(
                records.c.run == run.id, records.c.kind == "CHECKPOINT", records.c.unit == "search-spaces"
            )
        )
        if spaces is None and manifest.exists():
            spaces = json.loads(manifest.read_text(encoding="utf8"))
        for row in db.scalars(
            select(records.c.body).where(records.c.run == run.id, records.c.kind == "EVIDENCE")
        ):
            keys = {
                "text": row["evidence"]["claim"] if row["source"] != "sampled-frame" else None,
                "vision": (row.get("frame") or {}).get("sha256"),
            }
            for space, model in (spaces or {}).items():
                key = keys.get(space)
                if key:
                    retain.add(cache.vector_path(ws, model, key))
    for path in (cache.cache / ws).glob("??/*"):
        if re.fullmatch("[0-9a-f]{64}", path.name) and path not in retain and path.stat().st_mtime < cutoff:
            remove(path)
    return removed
