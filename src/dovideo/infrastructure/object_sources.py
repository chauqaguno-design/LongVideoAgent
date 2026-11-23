"""MinIO source bytes with a verified local decoder cache and durable SQL part receipts."""

from hashlib import sha256
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import HTTPException
from sqlalchemy import insert, select

from .media.source_artifacts import digest_file
from .persistence.object_parts_table import object_parts
from .persistence.source_tables import sources
from .upload_parts import UploadParts


class SourceObjects:
    def __init__(self, client, bucket):
        self.client, self.bucket = client, bucket

    @staticmethod
    def source_key(workspace, digest):
        return f"videomind/sources/{workspace}/{digest}"

    def put(self, key, path):
        self.client.fput_object(self.bucket, key, str(path))

    def materialize(self, scope, digest, path):
        return self.fetch(self.source_key(scope, digest), digest, path)

    def fetch(self, key, digest, path):
        if path.is_file():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(dir=path.parent, delete=False) as stage:
            staged = Path(stage.name)
        try:
            self.client.fget_object(self.bucket, key, str(staged))
            if digest_file(staged) != digest:
                raise HTTPException(503, "恢复的文件未通过摘要校验")
            try:
                os.link(staged, path)
            except FileExistsError:
                pass
            return path
        finally:
            staged.unlink(missing_ok=True)

    def remove_parts(self, scope, upload):
        self.remove_prefix(f"videomind/parts/{scope}/{upload}/")

    def remove_prefix(self, prefix):
        if not prefix.endswith("/"):
            raise ValueError("Object folder must end with /")
        for obj in self.client.list_objects(self.bucket, prefix=prefix, recursive=True):
            self.client.remove_object(self.bucket, obj.object_name)


class ObjectUploadParts(UploadParts):
    def __init__(self, store, row):
        super().__init__(store.root, row)
        self.row, self.store, self.objects = row, store, store.objects

    def key(self, number, digest):
        return f"videomind/parts/{self.row.workspace}/{self.row.upload}/{number}-{digest}"

    def save(self, number, staged, digest, size):
        start, end = self.bounds(number)
        if size != end - start:
            raise HTTPException(422, "分片大小与声明不符")
        # Serialize receipt assignment on the existing scoped upload row.
        with self.store.sessions.begin() as db:
            row = db.execute(
                select(sources)
                .where(sources.c.upload == self.row.upload, sources.c.workspace == self.row.workspace)
                .with_for_update()
            ).one()
            if row.receipt or (row.probe or {}).get("deleted"):
                raise HTTPException(409, "上传会话已结束")
            receipt = db.execute(
                select(object_parts.c.digest).where(
                    object_parts.c.upload == row.upload, object_parts.c.number == number
                )
            ).scalar_one_or_none()
            if receipt is not None:
                if receipt != digest:
                    raise HTTPException(409, "此分片已绑定其他内容")
                return
            self.objects.put(self.key(number, digest), staged)
            db.execute(
                insert(object_parts).values(
                    workspace=row.workspace, upload=row.upload, number=number, digest=digest, size=size
                )
            )

    def received(self):
        with self.store.sessions() as db:
            rows = db.execute(
                select(object_parts)
                .where(
                    object_parts.c.upload == self.row.upload, object_parts.c.workspace == self.row.workspace
                )
                .order_by(object_parts.c.number)
            )
            return [dict(part_number=r.number, etag=r.digest, size_bytes=r.size) for r in rows]

    def assemble(self, store, row, requested):
        expected = [dict(part_number=p["part_number"], etag=p["etag"]) for p in self.received()]
        if len(expected) != self.count or expected != requested:
            raise HTTPException(422, "分片缺失或凭据不符，请恢复上传")
        if row.digest:
            return expected
        total = sha256()
        with NamedTemporaryFile(dir=store.root, delete=False) as staged:
            temp = Path(staged.name)
        try:
            with temp.open("wb") as output:
                for part in expected:
                    digest = sha256()
                    key = self.key(part["part_number"], part["etag"])
                    incoming = self.objects.client.get_object(self.objects.bucket, key)
                    try:
                        for chunk in incoming.stream(1024 * 1024):
                            digest.update(chunk)
                            total.update(chunk)
                            output.write(chunk)
                    finally:
                        incoming.close()
                        incoming.release_conn()
                    if digest.hexdigest() != part["etag"]:
                        raise HTTPException(422, "分片内容与确认摘要不符")
            store.receive(row, temp, total.hexdigest(), temp.stat().st_size)
        finally:
            temp.unlink(missing_ok=True)
        return expected
