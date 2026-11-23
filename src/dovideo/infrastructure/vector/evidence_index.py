"""Qdrant points for immutable evidence. The serving generation supplies the allowlist."""

import asyncio
import hashlib
import re
import os
from threading import Lock

from .qdrant import QdrantVectorIndex, QdrantVectorError


def collection_name(model):
    return "vm_evidence_" + hashlib.sha256(model["version"].encode()).hexdigest()[:24]


def scope_filter(rows):
    first = rows[0]
    return {
        "must": [
            {"key": "workspace_id", "match": {"value": first["workspace_id"]}},
            {"key": "project_id", "match": {"value": first["project_id"]}},
            {"key": "media_revision_id", "match": {"any": sorted({r["media_revision_id"] for r in rows})}},
            {
                "key": "source_generation_id",
                "match": {"any": sorted({r["source_generation_id"] for r in rows})},
            },
            # Includes the exact project-generation components, query time window, and source deletion filter.
            {"has_id": [r["evidence_id"] for r in rows]},
        ]
    }


def point(row, vector):
    payload = {
        k: row[k]
        for k in ("workspace_id", "project_id", "media_revision_id", "source_generation_id", "segment_id")
    }
    payload.update(evidence_id=row["evidence_id"], **row["range"])
    return {"id": row["evidence_id"], "vector": list(vector), "payload": payload}


class EvidenceIndex:
    """Run against the existing HTTP port, or an explicitly selected embedded Qdrant directory."""

    def __init__(self, *, url=None, path=None):
        self.url = url
        self.api_key = os.getenv("VIDEOMIND_SEARCH_QDRANT_API_KEY")
        if not self.api_key and url and url.rstrip("/") == os.getenv("DOVIDEO_QDRANT_URL", "").rstrip("/"):
            self.api_key = os.getenv("DOVIDEO_QDRANT_API_KEY")
        self.local = None
        self.lock = Lock()
        if path:
            from qdrant_client import QdrantClient

            self.local = QdrantClient(path=str(path), force_disable_check_same_thread=True)

    def write(self, model, points):
        if not points:
            return
        collection = collection_name(model)
        if self.local:
            from qdrant_client import models

            with self.lock:
                if not self.local.collection_exists(collection):
                    self.local.create_collection(
                        collection,
                        vectors_config=models.VectorParams(
                            size=model["dimensions"], distance=models.Distance.COSINE
                        ),
                    )
                self.local.upsert(collection, points=[models.PointStruct(**p) for p in points], wait=True)
            return
        remote = QdrantVectorIndex(base_url=self.url, collection=collection, api_key=self.api_key)

        async def write():
            await remote._ensure_collection(model["dimensions"])
            response = await remote._send(
                "PUT", f"/collections/{collection}/points?wait=true", {"points": points}
            )
            if not 200 <= response.status_code < 300:
                raise QdrantVectorError("Scoped evidence upsert failed")

        asyncio.run(write())

    def rank(self, model, vector, rows, limit):
        if not rows:
            return []
        collection = collection_name(model)
        filters = scope_filter(rows)
        if self.local:
            from qdrant_client import models

            with self.lock:
                hits = self.local.query_points(
                    collection,
                    query=list(vector),
                    query_filter=models.Filter(**filters),
                    limit=limit,
                    with_payload=False,
                ).points
            return [(str(h.id), h.score) for h in hits]
        remote = QdrantVectorIndex(base_url=self.url, collection=collection, api_key=self.api_key)
        response = asyncio.run(
            remote._send(
                "POST",
                f"/collections/{collection}/points/query",
                {"query": list(vector), "filter": filters, "limit": limit, "with_payload": False},
            )
        )
        if not 200 <= response.status_code < 300:
            raise QdrantVectorError("Scoped evidence search failed")
        return [(h["id"], h["score"]) for h in response.body["result"]["points"]]

    def close(self):
        if self.local:
            self.local.close()

    def prune(self, workspace, project, revision, generations, apply):
        filters = {
            "must": [
                {"key": k, "match": {"value": v}}
                for k, v in (
                    ("workspace_id", workspace),
                    ("project_id", project),
                    ("media_revision_id", revision),
                )
            ]
            + [{"key": "source_generation_id", "match": {"any": generations}}]
        }
        matched = []

        def owned(name):
            return re.fullmatch(r"vm_evidence_[0-9a-f]{24}", name)

        if self.local:
            from qdrant_client import models

            scope = models.Filter(**filters)
            with self.lock:
                for c in self.local.get_collections().collections:
                    if owned(c.name) and self.local.count(c.name, count_filter=scope, exact=True).count:
                        matched.append(c.name)
                        if apply:
                            self.local.delete(
                                c.name, points_selector=models.FilterSelector(filter=scope), wait=True
                            )
        else:
            client = QdrantVectorIndex(base_url=self.url, collection="vm_evidence_gc", api_key=self.api_key)

            async def call(method, path, body=None):
                response = await client._send(method, path, body)
                if not 200 <= response.status_code < 300:
                    raise QdrantVectorError("Search retention request failed")
                return response.body["result"]

            async def prune_collections():
                for c in (await call("GET", "/collections"))["collections"]:
                    name = c["name"]
                    if not owned(name):
                        continue
                    path = f"/collections/{name}/points"
                    count = await call("POST", path + "/count", dict(filter=filters, exact=True))
                    if count["count"]:
                        matched.append(name)
                        if apply:
                            await call("POST", path + "/delete?wait=true", dict(filter=filters))

            asyncio.run(prune_collections())
        return [f"qdrant/{name}/{workspace}/{revision}" for name in matched]
