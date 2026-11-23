"""Reclaim superseded render attempts and duplicate completed-upload parts after retention."""
import time
from pathlib import Path

from dovideo.infrastructure.search_cleanup import cleanup_search

from sqlalchemy import select

from .edit_tables import artifacts, jobs, revisions
from .source_tables import sources
from .processing_tables import runs as processing_runs


def abandoned_media_files(root, db, cutoff):
    """Recover only scratch directories owned by a retired processing attempt."""
    for marker in (root / "processing").glob("tmp*/.attempt-*"):
        run_id, _, attempt = marker.name.removeprefix(".attempt-").rpartition("-")
        if not attempt.isdecimal() or marker.stat().st_mtime >= cutoff:
            continue
        row = db.execute(select(processing_runs.c.status, processing_runs.c.attempt)
                         .where(processing_runs.c.id == run_id)).first()
        if row and (row.attempt != int(attempt) or row.status not in ("QUEUED", "RUNNING")):
            yield from marker.parent.rglob("*")


def cleanup_files(store, *, older_than_seconds=86400, apply=False):
    if older_than_seconds < 0:
        raise ValueError("Retention must be nonnegative")
    root = store.root.resolve()
    cutoff = time.time() - older_than_seconds
    candidates = []
    remote = []
    objects = store.objects
    with store.sessions() as db:
        candidates.extend(abandoned_media_files(root, db, cutoff))
        # Successful uploads keep their receipt JSON. Future replays use the final immutable blob.
        completed = select(sources.c.workspace, sources.c.upload).where(sources.c.receipt.is_not(None))
        for scope, upload_id in db.execute(completed):
            candidates.extend((root / "upload-parts" / scope / upload_id).glob("*.part"))
            if objects:
                prefix = f"videomind/parts/{scope}/{upload_id}/"
                remote.extend(obj.object_name for obj in objects.client.list_objects(
                    objects.bucket, prefix=prefix, recursive=True) if obj.last_modified.timestamp() < cutoff)
        published = set(db.scalars(select(artifacts.c.relative_path)))
        media = db.execute(select(sources.c.id, sources.c.workspace, sources.c.revision, sources.c.digest, sources.c.probe)).all()
        revoked_sources = [row for row in media if (row.probe or {}).get("deleted_at", float("inf")) <= cutoff]
        deleted = {(row.workspace, row.revision) for row in revoked_sources}
        shared = {(r.workspace,r.digest) for r in media if not (r.probe or {}).get("deleted")}
        for row in revoked_sources:
            candidates.extend((root / "processing" / row.id).rglob("*"))
            if objects and row.digest and (row.workspace,row.digest) not in shared:
                candidates.append(store.blob(row.workspace,row.digest))
        render_jobs = select(jobs.c.id, jobs.c.attempt, jobs.c.status, jobs.c.workspace_id, revisions.c.snapshot).join(
            revisions, revisions.c.id == jobs.c.revision_id)
        for job_id, attempt, status, workspace, revision in db.execute(render_jobs):
            job_dir = root / "renders" / job_id
            revoked = any((workspace, clip["media_revision_id"]) in deleted for clip in revision["clips"])
            def reclaim(number):
                return number < attempt or (number == attempt and
                    (status in {"FAILED", "CANCELLED"} or (status == "SUCCEEDED" and revoked)))
            for folder in job_dir.glob("*"):
                if not folder.is_dir() or not folder.name.isdecimal():
                    continue
                if reclaim(int(folder.name)):
                    candidates.extend(path for path in folder.iterdir() if
                        revoked or path.relative_to(root / "renders").as_posix() not in published)
            if objects and (attempt > 1 or revoked or status in {"FAILED", "CANCELLED"}):
                prefix = f"videomind/renders/{workspace}/{job_id}/"
                for item in objects.client.list_objects(objects.bucket, prefix=prefix, recursive=True):
                    suffix = item.object_name.removeprefix(prefix)
                    number = suffix.partition("/")[0]
                    if (number.isdecimal() and reclaim(int(number)) and item.last_modified.timestamp() < cutoff
                            and (revoked or f"{job_id}/{suffix}" not in published)):
                        remote.append(item.object_name)
    removed = []
    for path in candidates:
        if (path.is_file() and not path.is_symlink() and path.resolve().is_relative_to(root)
                and path.stat().st_mtime < cutoff):
            if apply:
                path.unlink(missing_ok=True)
            removed.append(path.relative_to(root).as_posix())
    if apply and objects:
        for key in remote:
            objects.client.remove_object(objects.bucket, key)
    return removed + remote + cleanup_search(store, cutoff=cutoff, apply=apply)
