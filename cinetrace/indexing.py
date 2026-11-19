"""Build the optional Qdrant index from the same chunks the player displays."""

import hashlib
import importlib.util
import json
import os
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import NAMESPACE_URL, uuid5

from .media import ffmpeg


MODEL = "Qwen/Qwen3-VL-Embedding-2B"
MODEL_REVISION = "9f2f7e710d6d81056aa5c0a4f04764fec6bb7bda"
COLLECTION = "cinetrace_qwen3vl_2b_v1"


@lru_cache(maxsize=1)
def embedder():
    from huggingface_hub import snapshot_download

    directory = Path(os.getenv("CINETRACE_EMBED_MODEL") or snapshot_download(MODEL, revision=MODEL_REVISION))
    # Qwen ships this reference encoder alongside the weights in its model repository.
    script = directory / "scripts" / "qwen3_vl_embedding.py"
    if not script.is_file():
        raise FileNotFoundError(f"缺少 Qwen 官方编码器文件：{script}")
    spec = importlib.util.spec_from_file_location("cinetrace_qwen_embedding", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    class VideoEmbedder(module.Qwen3VLEmbedder):
        def _preprocess_inputs(self, conversations):
            batch = super()._preprocess_inputs(conversations)
            expects_video = any(item["type"] == "video" for conversation in conversations
                                for message in conversation for item in message["content"])
            # The reference helper falls back to placeholder text if media decoding fails.
            if expects_video and "pixel_values_videos" not in batch:
                raise ValueError("编码器未能读取视频帧，已停止写入画面向量。")
            return batch

    return VideoEmbedder(str(directory), num_frames=8, max_frames=8, dtype="auto")


def qdrant_client(url):
    from qdrant_client import QdrantClient

    if url == "local":
        root = Path(os.getenv("CINETRACE_DATA") or Path(__file__).resolve().parents[1] / "data")
        return QdrantClient(path=str(root / "qdrant"))
    return QdrantClient(url=url, timeout=30)


def build_index(path, work_dir, context):
    url = os.getenv("CINETRACE_QDRANT_URL")
    if not url:
        return {"status": "unavailable", "message": "未启用 Qdrant，已保留本地检索块。"}

    from qdrant_client import models

    chunks, evidence = context["chunks"], context["evidence"]
    rows = {e["id"]: e for e in evidence}
    revision = hashlib.sha256(json.dumps([MODEL_REVISION, "frames-8-v1", chunks, evidence],
                                         sort_keys=True).encode()).hexdigest()
    video_id = context["id"]
    count = 0
    client = qdrant_client(url)
    try:
        if not client.collection_exists(COLLECTION):
            client.create_collection(COLLECTION, vectors_config=models.VectorParams(
                size=2048, distance=models.Distance.COSINE))
        config = client.get_collection(COLLECTION).config.params.vectors
        if config.size != 2048 or config.distance != models.Distance.COSINE:
            raise ValueError("Qdrant 集合与 2048 维余弦配置不一致。")

        for chunk in chunks:
            with TemporaryDirectory(dir=work_dir) as temporary:
                inputs = {"frames": {}}
                for track in ("speech", "subtitles", "visual"):
                    texts = [rows[i]["text"] for i in chunk["evidence_ids"] if rows[i]["track"] == track]
                    if texts:
                        inputs[track] = {"text": "\n".join(texts)}
                ids = {s: str(uuid5(NAMESPACE_URL, f"{video_id}/{revision}/{chunk['id']}/{s}")) for s in inputs}
                present = {p.id for p in client.retrieve(COLLECTION, list(ids.values()),
                                                          with_payload=False, with_vectors=False)}
                missing = [s for s in inputs if ids[s] not in present]
                count += len(inputs)
                if not missing:
                    continue
                if "frames" in missing:
                    ffmpeg("-ss", chunk["start_ms"] / 1000, "-i", path, "-an",
                           "-t", (chunk["end_ms"] - chunk["start_ms"]) / 1000,
                           "-vf", "fps=1:round=up,scale=-2:224", Path(temporary) / "%06d.jpg")
                    frames = [str(p.resolve()) for p in sorted(Path(temporary).glob("*.jpg"))]
                    if not frames:
                        raise ValueError("检索块未能解码出画面。")
                    inputs["frames"] = {"video": frames}
                vectors = embedder().process([inputs[s] for s in missing]).tolist()
                payload = {"video_id": video_id, "version": revision, "chunk_id": chunk["id"],
                           "start_ms": chunk["start_ms"], "end_ms": chunk["end_ms"]}
                points = [models.PointStruct(id=ids[s], vector=v, payload={**payload, "source": s})
                          for s, v in zip(missing, vectors, strict=True)]
                client.upsert(COLLECTION, points, wait=True)
                if len(client.retrieve(COLLECTION, list(ids.values()), with_payload=False,
                                       with_vectors=False)) != len(ids):
                    raise RuntimeError("部分向量尚未写入，可重试此任务。")
    finally:
        client.close()
    return {"status": "ready", "message": f"已索引 {count} 条向量", "collection": COLLECTION,
            "model": MODEL, "version": revision, "points": count}


def search_index(video_id, version, text, source=None, limit=5):
    """Offline acceptance query. Return the top distinct chunks of this video version."""
    from qdrant_client import models

    url = os.getenv("CINETRACE_QDRANT_URL")
    if not url:
        raise ValueError("请设置 CINETRACE_QDRANT_URL。")
    query = embedder().process([{"text": text, "instruction": "Retrieve video clips relevant to the query."}])
    filters = {"video_id": video_id, "version": version}
    if source:
        filters["source"] = source
    conditions = [models.FieldCondition(key=k, match=models.MatchValue(value=v)) for k, v in filters.items()]
    client = qdrant_client(url)
    try:
        # Embedded Qdrant always runs an exact search.
        params = None if url == "local" else models.SearchParams(exact=True)
        hits = client.query_points(COLLECTION, query=query[0].tolist(),
                                   query_filter=models.Filter(must=conditions), search_params=params,
                                   limit=limit * 4, with_payload=True).points
    finally:
        client.close()
    chunks = {}
    for hit in hits:
        chunks.setdefault(hit.payload["chunk_id"], {**hit.payload, "score": hit.score})
    return list(chunks.values())[:limit]


if __name__ == "__main__":
    import argparse
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
    parser = argparse.ArgumentParser(description="在一个已构建的影片版本中检索前五个片段。")
    parser.add_argument("video_id")
    parser.add_argument("version", help="视频详情中 index.version 的值")
    parser.add_argument("query")
    parser.add_argument("--source", choices=["frames", "visual", "speech", "subtitles"])
    args = parser.parse_args()
    print(json.dumps(search_index(args.video_id, args.version, args.query, args.source),
                     ensure_ascii=False, indent=2))
