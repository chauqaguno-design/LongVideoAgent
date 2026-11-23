"""Local multipart uploads with immutable part bytes and resumable receipts."""
import hashlib
import json
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import HTTPException


MIB = 1024 * 1024
MIN_PART_SIZE = 8 * MIB


def part_size(total):
    # Keep even the largest accepted upload within the 1,000-part refresh contract.
    return max(MIN_PART_SIZE, math.ceil(total / (1000 * MIB)) * MIB)


class UploadParts:
    def __init__(self, root, row):
        self.folder = root / "upload-parts" / row.workspace / row.upload
        self.size = row.size
        self.chunk = part_size(row.size)
        self.count = math.ceil(row.size / self.chunk)

    def bounds(self, number):
        if not 1 <= number <= self.count:
            raise HTTPException(422, "分片编号超出范围")
        start = (number - 1) * self.chunk
        return start, min(start + self.chunk, self.size)

    def save(self, number, staged, digest, size):
        start, end = self.bounds(number)
        if size != end - start:
            raise HTTPException(422, "分片大小与声明不符")
        self.folder.mkdir(parents=True, exist_ok=True)
        target = self.folder / f"{number}.part"
        try:
            os.link(staged, target)
        except FileExistsError:
            with target.open("rb") as existing:
                if hashlib.file_digest(existing, "sha256").hexdigest() != digest:
                    raise HTTPException(409, "此分片已绑定其他内容") from None
        receipt = dict(part_number=number, etag=digest, size_bytes=size)
        with NamedTemporaryFile(mode="w", dir=self.folder, encoding="utf8", delete=False) as temp:
            json.dump(receipt, temp)
        Path(temp.name).replace(self.folder / f"{number}.json")

    def received(self):
        rows = [json.loads(path.read_text(encoding="utf8")) for path in self.folder.glob("*.json")]
        return sorted(rows, key=lambda p: p["part_number"])

    def assemble(self, store, row, requested):
        expected = [dict(part_number=p["part_number"], etag=p["etag"]) for p in self.received()]
        if len(expected) != self.count or requested != expected:
            raise HTTPException(422, "分片缺失或凭据不符，请恢复上传")
        if row.digest:
            return expected
        digest = hashlib.sha256()
        with NamedTemporaryFile(dir=store.root, delete=False) as output:
            temp = Path(output.name)
        try:
            with temp.open("wb") as output:
                for part in expected:
                    with (self.folder / f"{part['part_number']}.part").open("rb") as data:
                        while chunk := data.read(MIB):
                            output.write(chunk)
                            digest.update(chunk)
            store.receive(row, temp, digest.hexdigest(), temp.stat().st_size)
        finally:
            temp.unlink(missing_ok=True)
        return expected
