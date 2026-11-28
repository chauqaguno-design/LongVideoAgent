"""Optional trained encoders on a local, explicitly configured embedding service."""

from array import array
from contextlib import nullcontext
import base64
import hashlib
from http.client import HTTPException as HTTPTransportError
from io import BytesIO
import json
import logging
import math
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener

from PIL import Image
from cutsieve.application.rank_fusion import RankedCandidate
from cutsieve.infrastructure.vector.evidence_index import EvidenceIndex, point
from cutsieve.infrastructure.vector.qdrant import QdrantVectorError


def local_encoder(root):
    url = os.getenv("CUTSIEVE_SEARCH_ENCODER_URL")
    if not url:
        return None
    addr = urlsplit(url)
    if addr.scheme != "http" or addr.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise ValueError("本地编码器应使用回环 HTTP 地址")
    qdrant_url = os.getenv("CUTSIEVE_SEARCH_QDRANT_URL")
    qdrant_path = os.getenv("CUTSIEVE_SEARCH_QDRANT_PATH")
    index = None
    if qdrant_url or qdrant_path:
        try:
            index = EvidenceIndex(url=qdrant_url, path=qdrant_path)
        except ImportError:
            logging.getLogger(__name__).warning("Embedded Qdrant unavailable; using local vector caching")
    return SearchEncoder(url.rstrip("/"), root / "embeddings", index=index)


def image_input(image):
    # Transport normalized pixels only; the model server never receives a source path.
    with BytesIO() as f:
        pixels = image.copy()
        pixels.thumbnail((384, 384), Image.Resampling.LANCZOS)
        pixels.convert("RGB").save(f, format="PNG")
        return {"image": base64.b64encode(f.getvalue()).decode()}


class SearchEncoder:
    def __init__(self, url, cache, *, index=None):
        self.url, self.cache = url, Path(cache)
        self.index = index
        self.http = build_opener(ProxyHandler({}))

    def call(self, path, body=None, *, timeout=25):
        data = None if body is None else json.dumps(body).encode()
        req = Request(self.url + path, data=data, headers={"Content-Type": "application/json"})
        try:
            with self.http.open(req, timeout=timeout) as response:
                return json.load(response)
        except HTTPTransportError as exc:
            raise OSError("本地编码器的 HTTP 响应不完整") from exc

    def spaces(self, **request_options):
        advertised = self.call("/spaces", **request_options)
        if not isinstance(advertised, dict):
            raise ValueError("编码空间清单无效")
        spaces = {}
        for name, model in advertised.items():
            if name not in {"text", "vision"}:
                continue
            if not isinstance(model, dict):
                raise ValueError("编码空间描述无效")
            version, dimensions = model.get("version"), model.get("dimensions")
            if not isinstance(version, str) or not version.strip() or type(dimensions) is not int or dimensions < 1:
                raise ValueError("编码空间版本或维度无效")
            spaces[name] = model
        return spaces

    def vector_path(self, workspace, model, key):
        digest = hashlib.sha256((model["version"] + "|" + key).encode()).hexdigest()
        return self.cache / workspace / digest[:2] / digest

    def generation_path(self, workspace, component):
        digest = hashlib.sha256(component.encode()).hexdigest()
        return self.cache / workspace / "generations" / (digest + ".json")

    @staticmethod
    def feature(row, space):
        if space == "text" and row.get("source") != "sampled-frame":
            return row["claim"], {"text": row["claim"]}
        if space == "vision" and row.get("frame") and row.get("path"):
            return row["frame"]["sha256"], row["path"]
        return None, None

    def encode(self, space, model, items):
        result = self.call("/encode", {"space": space, "version": model["version"], "items": items})
        vectors = result["vectors"]
        if (
            result["version"] != model["version"]
            or len(vectors) != len(items)
            or any(len(v) != model["dimensions"] or not all(map(math.isfinite, v)) for v in vectors)
        ):
            raise ValueError("编码空间或维度有误")
        return vectors

    def prepare(self, workspace, rows, *, publication=nullcontext):
        spaces = self.spaces()
        prepared = {}
        for space, model in spaces.items():
            pending = {}
            for row in rows:
                key, value = self.feature(row, space)
                if key:
                    prepared[space] = model
                    path = self.vector_path(workspace, model, key)
                    if not path.is_file():
                        pending[path] = value
            values = list(pending.items())
            for start in range(0, len(values), 4):
                batch, items = values[start : start + 4], []
                for _, value in batch:
                    if space == "vision":
                        with Image.open(value) as image:
                            items.append(image_input(image))
                    else:
                        items.append(value)
                encoded = self.encode(space, model, items)
                with publication():
                    for (target, _), vec in zip(batch, encoded, strict=True):
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with NamedTemporaryFile(dir=target.parent, delete=False) as f:
                            f.write(array("f", vec).tobytes())
                        os.replace(f.name, target)
            if self.index:
                batch = []
                for row in rows:
                    key, _ = self.feature(row, space)
                    if not key:
                        continue
                    vec = array("f")
                    vec.frombytes(self.vector_path(workspace, model, key).read_bytes())
                    batch.append(point(row, vec))
                    if len(batch) == 64:
                        with publication():
                            self.index.write(model, batch)
                        batch = []
                if batch:
                    with publication():
                        self.index.write(model, batch)
        with publication():
            self.freeze_spaces(workspace, rows, prepared)
        return prepared

    def freeze_spaces(self, workspace, rows, prepared):
        for component in {r["source_generation_id"] for r in rows}:
            path = self.generation_path(workspace, component)
            path.parent.mkdir(parents=True, exist_ok=True)
            with NamedTemporaryFile(mode="w", encoding="utf8", dir=path.parent, delete=False) as f:
                json.dump(prepared, f, sort_keys=True)
            try:
                try:
                    os.link(f.name, path)
                except FileExistsError:
                    if json.loads(path.read_text(encoding="utf8")) != prepared:
                        raise ValueError("已冻结的索引版本不可更换编码器")
            finally:
                Path(f.name).unlink(missing_ok=True)

    def rank(self, text, image, rows, routes, warnings):
        try:
            manifests = {}
            for row in rows:
                component = row["source_generation_id"]
                if component not in manifests:
                    path = self.generation_path(row["workspace_id"], component)
                    # SQL owns the published descriptor; legacy local generations use their frozen file.
                    manifests[component] = row.get("search_spaces") or (
                        json.loads(path.read_text(encoding="utf8")) if path.exists() else {}
                    )
            for space, model in self.spaces().items():
                if space == "vision" and image is not None:
                    query = image_input(image)
                elif text:
                    query = {"text": text}
                else:
                    continue
                eligible = [(i, r) for i, r in enumerate(rows) if self.feature(r, space)[0]]
                pinned = [
                    (i, r) for i, r in eligible if manifests[r["source_generation_id"]].get(space) == model
                ]
                if len(pinned) < len(eligible):
                    warnings.append(f"{space} 部分索引版本与已部署编码空间不一致或尚未建成")
                if self.index:
                    if not pinned:
                        continue
                    mapping = {r["evidence_id"]: i for i, r in pinned}
                    q = self.encode(space, model, [query])[0]
                    neighbors = self.index.rank(model, q, [r for _, r in pinned], min(200, len(pinned)))
                    ranked = [RankedCandidate(mapping[eid], score) for eid, score in neighbors]
                    routes[space + "_qdrant"] = (model["version"], ranked)
                    if not ranked:
                        warnings.append(f"{space} 当前授权快照尚无可检索向量")
                    continue
                # Only use projections published for these source rows and for this fingerprint.
                vectors = []
                missing = 0
                for i, row in pinned:
                    key, _ = self.feature(row, space)
                    if not key:
                        continue
                    path = self.vector_path(row["workspace_id"], model, key)
                    if not path.is_file():
                        missing += 1
                        continue
                    vec = array("f")
                    vec.frombytes(path.read_bytes())
                    if len(vec) != model["dimensions"]:
                        raise ValueError("向量缓存维度不匹配")
                    vectors.append((i, vec))
                if missing:
                    warnings.append(f"{space} 编码路有 {missing} 个已授权来源尚未按当前权重建立向量")
                if vectors:
                    q = self.encode(space, model, [query])[0]
                    scored = [RankedCandidate(i, sum(x * y for x, y in zip(q, v))) for i, v in vectors]
                    routes[space + "_dense"] = (
                        model["version"],
                        sorted(scored, key=lambda x: (-x.score, x.index)),
                    )
        except (OSError, ValueError, KeyError, QdrantVectorError) as exc:
            warnings.append(f"本地语义编码器不可用（{type(exc).__name__}）")
