"""Private content-addressed files for screenshot queries and complete result lists."""

import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .persistence.search_tables import artifacts, runs
from .search_cleanup import scoped_write


class SearchArtifacts:
    def __init__(self, sources):
        self.sources = sources
        self.root = sources.root / "query-files"

    @staticmethod
    def object_key(workspace, digest):
        return f"videomind/query-files/{workspace}/{digest}"

    def path(self, row):
        path = self.root / row.workspace / row.digest
        if self.sources.objects and not path.exists():
            with scoped_write(self.sources, row.workspace, row.media_ids) as db:
                if (
                    db.scalar(
                        select(artifacts.c.id).where(
                            artifacts.c.workspace == row.workspace, artifacts.c.id == row.id
                        )
                    )
                    is None
                ):
                    raise HTTPException(404, "查询文件不存在")
                return self.sources.objects.fetch(
                    self.object_key(row.workspace, row.digest), row.digest, path
                )
        return path

    def put_bytes(self, workspace, digest, data):
        directory = self.root / workspace
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / digest
        if not target.is_file():
            with NamedTemporaryFile(dir=directory, delete=False) as f:
                f.write(data)
            try:
                os.replace(f.name, target)
            finally:
                Path(f.name).unlink(missing_ok=True)
        if self.sources.objects:
            self.sources.objects.put(self.object_key(workspace, digest), target)

    def save(self, workspace, project, data, kind, mime, media_ids=(), *, ident=None):
        digest = hashlib.sha256(data).hexdigest()
        ident = ident or str(uuid4())
        try:
            with scoped_write(self.sources, workspace, media_ids) as db:
                self.put_bytes(workspace, digest, data)
                db.execute(
                    artifacts.insert().values(
                        id=ident,
                        workspace=workspace,
                        project=project,
                        kind=kind,
                        digest=digest,
                        mime=mime,
                        size=len(data),
                        media_ids=list(media_ids),
                    )
                )
        except IntegrityError:
            existing = self.get(workspace, ident, project=project)
            if not existing or existing.digest != digest:
                raise
        return ident

    def get(self, workspace, ident, *, project=None):
        query = select(artifacts).where(artifacts.c.workspace == workspace, artifacts.c.id == ident)
        if project is not None:
            query = query.where(artifacts.c.project == project)
        with self.sources.sessions() as db:
            row = db.execute(query).first()
        if row is None:
            return None
        self.sources.project(workspace, row.project)
        for revision_id in row.media_ids:
            self.sources.media(workspace, revision_id)
        return row

    def image(self, workspace, project, ident):
        row = self.get(workspace, ident, project=project)
        if not row or row.kind != "IMAGE_QUERY":
            raise HTTPException(404, "查询图片不存在")
        return self.path(row).read_bytes()

    def occurrences(self, workspace, project, ident):
        row = self.get(workspace, ident, project=project)
        if not row or row.kind != "QUERY_RESULT":
            raise HTTPException(404, "结果清单不存在")
        with self.path(row).open(encoding="utf8") as f:
            return json.load(f)["events"]

    def verified(self, workspace, ident):
        row = self.get(workspace, ident)
        if row and row.kind == "QUERY_EVIDENCE":
            with self.path(row).open(encoding="utf8") as f:
                return json.load(f)["evidence"]
        return None

    def roi(self, workspace, project, image_id):
        ident = str(uuid5(UUID(image_id), "roi-provenance"))
        row = self.get(workspace, ident, project=project)
        if row and row.kind == "QUERY_IMAGE_META":
            return json.loads(self.path(row).read_text(encoding="utf8"))
        return None

    def scan(self, workspace, run_id, request_hash):
        with self.sources.sessions() as db:
            row = db.execute(
                select(runs).where(runs.c.workspace == workspace, runs.c.run_id == run_id)
            ).first()
        if row:
            if row.request_hash != request_hash:
                raise HTTPException(409, "核查任务已绑定其他问题")
            self.sources.project(workspace, row.project)
            for item in row.snapshot["requested"]:
                self.sources.media(workspace, item["media_revision_id"])
        return row

    def freeze(self, workspace, run_id, request_hash, snapshot):
        try:
            with scoped_write(
                self.sources, workspace, [r["media_revision_id"] for r in snapshot["requested"]]
            ) as db:
                db.execute(
                    runs.insert().values(
                        run_id=run_id,
                        workspace=workspace,
                        project=snapshot["project"],
                        request_hash=request_hash,
                        snapshot=snapshot,
                    )
                )
        except IntegrityError:
            if not self.scan(workspace, run_id, request_hash):
                raise
        return self.scan(workspace, run_id, request_hash)

    def finish(self, workspace, run_id, answer):
        with self.sources.sessions.begin() as db:
            db.execute(
                runs.update()
                .where(runs.c.run_id == run_id, runs.c.workspace == workspace)
                .values(answer=answer)
            )
