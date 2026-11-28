from contextlib import nullcontext
import importlib.util
import json
import math
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
from PIL import Image
import pytest

from cutsieve.infrastructure.edit_media import media_binaries
from cutsieve.infrastructure.media.processing_units import ProcessingUnits
from cutsieve.infrastructure.search_encoder import SearchEncoder, image_input
from cutsieve.infrastructure.vector.evidence_index import EvidenceIndex, collection_name
from cutsieve.infrastructure.vector.qdrant import JsonHttpResponse, QdrantVectorIndex
from cutsieve.presentation.api.app import create_app
from scripts.eval_harness import body, ingest, synthetic_video, wait_index


def test_same_dimensions_do_not_authorize_another_checkpoint(tmp_path, monkeypatch):
    encoder = SearchEncoder("http://127.0.0.1:18174", tmp_path)
    model = {"dimensions": 3, "version": "checkpoint-A"}
    count = []

    def exchange(path, body=None, *, timeout=25):
        if path == "/spaces":
            return {"text": dict(model)}
        count.append(len(body["items"]))
        return {
            "version": model["version"],
            "vectors": [[1, 0, 0] for item in body["items"]],
        }

    monkeypatch.setattr(encoder, "call", exchange)
    row = {
        "workspace_id": "workspace",
        "source_generation_id": "immutable-run",
        "source": "embedded-subtitle",
        "claim": "the red car",
    }
    encoder.prepare("workspace", [row])
    encoder.prepare("workspace", [row])
    assert count == [1]
    routes = {}
    encoder.rank("red car", None, [row], routes, [])
    assert routes["text_dense"][1][0].score == 1
    model["version"] = "checkpoint-B"
    routes, warnings = {}, []
    encoder.rank("red car", None, [row], routes, warnings)
    assert routes == {} and warnings


@pytest.mark.parametrize(("model_type", "dimension"), [("chinese_clip", 512), ("clip", 512), ("siglip", 768)])
def test_local_clip_encodes_both_modalities_in_one_normalized_space(tmp_path, monkeypatch, model_type, dimension):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    script = Path(__file__).resolve().parents[2] / "scripts" / "serve_search_encoder.py"
    spec = importlib.util.spec_from_file_location("serve_search_encoder", script)
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)

    class Tensor(list):
        def float(self):
            return self

        def tolist(self):
            return list(self)

        @property
        def shape(self):
            return len(self), len(self[0])

    def normalize(vectors, *, dim):
        assert dim == -1
        return [[value / math.hypot(*vector) for value in vector] for vector in vectors]

    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False), inference_mode=nullcontext, stack=Tensor,
        nn=SimpleNamespace(functional=SimpleNamespace(normalize=normalize)),
    ))

    class Model:
        config = SimpleNamespace(
            projection_dim=512, vision_config=SimpleNamespace(hidden_size=768),
            text_config=SimpleNamespace(max_position_embeddings=77),
        )
        text_model = SimpleNamespace(register_forward_hook=lambda hook: None)

        def to(self, device):
            assert device == "cpu"
            return self

        def eval(self):
            return self

        def get_image_features(self, pixel_values):
            return SimpleNamespace(pooler_output=Tensor([[3, 4] + [0] * (dimension - 2) for _ in pixel_values]))

        def get_text_features(self, input_ids):
            return Tensor([[0, 5] + [0] * (dimension - 2) for _ in input_ids])

    class Inputs(dict):
        def to(self, device):
            assert device == "cpu"
            return self

    def processor(*, return_tensors, images=None, text=None, **options):
        assert return_tensors == "pt"
        if images:
            assert all(image.mode == "RGB" for image in images)
            return Inputs(pixel_values=images)
        assert text == ["龙飞过雪山"] * len(text)
        assert options == dict(padding="max_length" if model_type == "siglip" else True,
                               truncation=True, max_length=77)
        return Inputs(input_ids=text)

    def loader(value):
        def load(folder, *, local_files_only):
            assert local_files_only and folder == tmp_path
            return value

        return SimpleNamespace(from_pretrained=load)

    models = dict(ChineseCLIPModel=loader(Model()), ChineseCLIPProcessor=loader(processor)) if model_type == "chinese_clip" else {
        "AutoModel": loader(Model()), "AutoProcessor": loader(processor),
    }
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(**models))
    (tmp_path / "config.json").write_text(json.dumps({"model_type": model_type}), encoding="utf-8")
    encoders = server.Encoders(tmp_path, None)
    assert set(encoders.spaces) == {"vision"}
    assert encoders.spaces["vision"] == dict(
        version=f"{tmp_path.name}-{server.fingerprint(tmp_path)}", dimensions=dimension,
    )
    image = image_input(Image.new("RGB", (4, 4)))
    items = [{"text": "龙飞过雪山"}, image] * 8
    payload = dict(space="vision", version=encoders.spaces["vision"]["version"], items=items)
    vectors = encoders.encode(payload)["vectors"]
    assert [len(v) for v in vectors] == [dimension] * 16
    assert [math.hypot(*v) for v in vectors] == pytest.approx([1] * 16)
    assert [v[:2] for v in vectors] == [[0, 1], [0.6, 0.8]] * 8
    with pytest.raises(ValueError):
        encoders.encode(dict(payload, items=items + [image]))


def test_text_query_retrieves_vision_points_with_scoped_512_and_1024_collections(tmp_path, monkeypatch):
    spaces = dict(vision=dict(version="chinese-clip", dimensions=512), text=dict(version="bge-m3", dimensions=1024))
    dimensions = {}
    stored = {}
    query_items = []

    async def send(self, method, path, body):
        collection = self.collection
        if method == "GET":
            return JsonHttpResponse(404, {})
        if "points" not in path:
            dimensions[collection] = body["vectors"]["size"]
        elif method == "PUT":
            stored[collection] = body["points"]
            assert all(len(point["vector"]) == dimensions[collection] for point in body["points"])
        else:
            assert len(body["query"]) == dimensions[collection]
            point = stored[collection][0]
            assert {"has_id": [point["id"]]} in body["filter"]["must"]
            assert {"key": "workspace_id", "match": {"value": "workspace"}} in body["filter"]["must"]
            return JsonHttpResponse(200, {"result": {"points": [dict(id=point["id"], score=1)]}})
        return JsonHttpResponse(200, {})

    def call(path, body=None, *, timeout=25):
        if path == "/spaces":
            return spaces
        model = spaces[body["space"]]
        query_items.append((body["space"], body["items"][0]))
        return dict(version=model["version"], vectors=[[1] + [0] * (model["dimensions"] - 1)])

    monkeypatch.setattr(QdrantVectorIndex, "_send", send)
    index = EvidenceIndex(url="http://127.0.0.1:6333")
    encoder = SearchEncoder("http://127.0.0.1:8174", tmp_path / "cache", index=index)
    monkeypatch.setattr(encoder, "call", call)
    image = tmp_path / "frame.jpg"
    Image.new("RGB", (4, 4)).save(image)
    base = dict(workspace_id="workspace", project_id="project", media_revision_id="revision",
                source_generation_id="generation", segment_id="segment", range=dict(start_us=0, end_us=1))
    rows = [
        dict(base, evidence_id="frame", source="sampled-frame", frame=dict(sha256="frame"), path=image),
        dict(base, evidence_id="subtitle", source="embedded-subtitle", claim="台词"),
    ]
    encoder.prepare("workspace", rows)
    assert dimensions == {collection_name(model): model["dimensions"] for model in spaces.values()}
    assert "image" in query_items[0][1]
    routes, warnings = {}, []
    encoder.rank("桥上争吵", None, rows, routes, warnings)
    assert query_items[-2:] == [(space, {"text": "桥上争吵"}) for space in ("vision", "text")]
    assert routes["vision_qdrant"][1][0].index == 0
    assert routes["text_qdrant"][1][0].index == 1
    assert not warnings


@pytest.mark.real_media
def test_vision_only_ingest_publishes_fts5_and_embedded_qdrant(tmp_path, monkeypatch):
    pytest.importorskip("qdrant_client")
    if not all(shutil.which(exe) for exe in media_binaries().values()):
        pytest.skip("FFmpeg and ffprobe required")
    monkeypatch.setenv("CUTSIEVE_SEARCH_ENCODER_URL", "http://127.0.0.1:18176")
    monkeypatch.setenv("CUTSIEVE_SEARCH_QDRANT_PATH", str(tmp_path / "qdrant"))
    monkeypatch.delenv("CUTSIEVE_SEARCH_QDRANT_URL", raising=False)
    monkeypatch.delenv("CUTSIEVE_MEDIA_AUTO_VISUAL", raising=False)
    model = dict(version="chinese-clip", dimensions=512)
    encoded = []

    def call(self, path, payload=None, *, timeout=25):
        if path == "/spaces":
            return {"vision": model}
        assert path == "/encode" and payload["space"] == "vision"
        assert payload["version"] == model["version"]
        encoded.extend(payload["items"])
        return dict(version=model["version"], vectors=[[1] + [0] * 511 for _ in payload["items"]])

    def local_batch(self, mode):
        assert mode == "ocr"  # Usable embedded subtitles must bypass ASR, even with an audio track.
        return []

    monkeypatch.setattr(SearchEncoder, "call", call)
    monkeypatch.setattr(ProcessingUnits, "local_batch", local_batch)
    video = synthetic_video(tmp_path)
    with TestClient(create_app(work_dir=tmp_path / "store")) as client:
        caps = body(client.get("/v1/capabilities"))["dedicated_services"]
        embedding = next(c for c in caps if c["name"] == "embedding")
        assert embedding == dict(name="embedding", configured=True, ready=True, verified=True,
                                 version="chinese-clip", versions={"vision": "chinese-clip"})
        project, asset = ingest(client, video, tmp_path)
        wait_index(client, asset, 25)
        asset_url = f"/v1/assets/{asset['asset_id']}"
        run, = body(client.get(asset_url + "/processing-runs"))["items"]
        assert (run["status"], run["error"]) == ("SUCCEEDED", None)
        stages = {c["name"]: c for c in body(client.get(asset_url))["capabilities"]}
        assert stages["asr"] == dict(name="asr", state="SKIPPED", reason="内嵌字幕已覆盖")
        assert stages["text_embedding"] == dict(name="text_embedding", state="SKIPPED",
                                                reason="默认由 FTS5 全文检索覆盖（未单独启用文本向量）")
        assert stages["image_embedding"] == dict(name="image_embedding", state="AVAILABLE",
                                                 reason=None, storage="qdrant")
        assert encoded and all("image" in item for item in encoded)

        result = body(client.post("/v1/search", json=dict(
            scope=dict(project_id=project), text="机器人", query_mode="LOCAL", top_k=20,
        )))
        assert encoded[-1] == {"text": "机器人"}
        routes = {rank["route"] for hit in result["hits"] for rank in hit["route_ranks"]}
        assert routes == {"fts5", "verbatim", "vision_qdrant"}
        assert any(hit["range"]["start_us"] >= 8_000_000 and
                   any(rank["route"] == "vision_qdrant" for rank in hit["route_ranks"])
                   for hit in result["hits"])
        assert result["warnings"] == []
