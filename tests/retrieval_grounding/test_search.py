"""Project isolation, pinned search snapshots, real pixel matching and global answer semantics."""

from io import BytesIO
import json
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
import pytest
from sqlalchemy import create_engine

from dovideo.application.edit_sources import EditSources
from dovideo.application.evidence_search import EvidenceSearch
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.infrastructure.persistence.search_tables import artifacts, runs
from dovideo.presentation.api.search import install_search_routes


def uid():
    return str(uuid4())


class Catalog:
    def __init__(self):
        self.gen, self.rows, self.checked = uid(), [], {}
        self.history = {}

    def search_scope(self, ws, project, media, *, generation_id=None):
        generation = generation_id or self.gen
        rows = self.rows if generation == self.gen else self.history[generation]
        return generation, [
            r
            for r in rows
            if r["workspace_id"] == ws and r["project_id"] == project and r["media_revision_id"] in media
        ]

    def literal_coverage(self, ws, project, gen, media):
        assert gen == self.gen
        return self.checked


@pytest.fixture
def ctx(tmp_path):
    db = create_engine(f"sqlite:///{tmp_path / 'sources.db'}")
    for table in (projects, sources, artifacts, runs):
        table.create(db)
    store = EditSources(db, tmp_path)
    ws = store.workspace({"id": 1})
    catalog = Catalog()
    service = EvidenceSearch(store, catalog)
    project = store.create_project(ws, "影片")["project_id"]

    def media(scope=ws, pid=project):
        ids = {k: uid() for k in ("id", "revision", "upload", "artifact")}
        with db.begin() as con:
            con.execute(
                sources.insert().values(
                    **ids,
                    workspace=scope,
                    project=pid,
                    filename="样片.mp4",
                    size=10,
                    mime="video/mp4",
                    digest="a" * 64,
                    receipt="b" * 64,
                    expires=2_000_000_000,
                    probe=dict(
                        duration_us=3_600_000_000,
                        browser_playable=True,
                        streams=[],
                        source_origin={"num": 0, "den": 1},
                    ),
                )
            )
        return ids["revision"]

    mid = media()

    def evidence(
        text,
        t,
        *,
        end=None,
        mid=mid,
        scope=ws,
        pid=project,
        source="embedded-subtitle",
        frame=None,
        path=None,
    ):
        row = dict(
            evidence_id=uid(),
            workspace_id=scope,
            project_id=pid,
            media_revision_id=mid,
            range=dict(start_us=t, end_us=end or t + 1_000_000),
            claim=text,
            support="SUPPORTED",
            observation_ids=[uid()],
            artifact_ids=[uid()],
            verification_protocol="read-source",
            index_generation_id=catalog.gen,
            source=source,
            modality="VISUAL" if frame else "OCR",
            frame=frame,
            path=path,
            segment_id=uid(),
        )
        catalog.rows.append(row)
        return row

    query = dict(scope={"project_id": project}, query_mode="LOCAL", text="接住那把钥匙", top_k=5)
    yield SimpleNamespace(
        service=service,
        store=store,
        catalog=catalog,
        ws=ws,
        project=project,
        mid=mid,
        query=query,
        media=media,
        evidence=evidence,
        folder=tmp_path,
    )
    db.dispose()


def test_query_filters_before_top_k_and_rejects_foreign_revision(ctx):
    c = ctx
    known = c.evidence("请你接住那把钥匙", 10)
    other_ws = c.store.workspace({"id": 2})
    foreign = c.store.create_project(other_ws, "另一个项目")["project_id"]
    other = c.media(other_ws, foreign)
    for _ in range(25):
        c.evidence(c.query["text"], 20, mid=other, scope=other_ws, pid=foreign)
    result = c.service.search(c.ws, dict(c.query, top_k=1))
    validate_contract("SearchResult", result)
    (hit,) = result["hits"]
    assert hit["verified"] is False and hit["evidence_ids"] == [known["evidence_id"]]
    assert result["candidate_count"] == 1 and hit["route_ranks"][0]["route"] == "bm25"
    with pytest.raises(HTTPException) as exc:
        c.service.search(
            c.ws,
            dict(c.query, scope=dict(project_id=c.project, media_revision_ids=[other])),
        )
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        c.service.search(c.ws, dict(c.query, range=dict(start_us=12, end_us=10)))
    c.media()
    with pytest.raises(HTTPException, match="一部视频"):
        c.service.search(c.ws, dict(c.query, query_mode="FIRST"))


def test_grounded_quotation_excludes_unrelated_nearby_claims(ctx):
    spoken = ctx.evidence("Wait a minute.", 8_900_000, source="faster-whisper")
    nearby = ctx.evidence("原片采样画面", 8_901_000, source="sampled-frame")
    request = dict(ctx.query, text="哪一段说了「Wait a minute.」？")
    plan = ctx.service.plan(ctx.ws, request)
    candidates = {"hits": [{"evidence_ids": [spoken["evidence_id"], nearby["evidence_id"]]}],
                  "warnings": [], "query_id": uid()}
    result = ctx.service.ground(ctx.ws, uid(), request, plan, candidates, {})
    assert result["claims"] == [
        {"text": "Wait a minute.", "evidence_ids": [spoken["evidence_id"]], "support": "SUPPORTED"}
    ]


def test_global_literal_evidence_beyond_top_k_and_unverified_prefix(ctx):
    c = ctx
    for t in (40_000_000, 2_700_000_000, 2_703_000_000):
        c.evidence("接住那把钥匙", t)
    c.evidence("接住那把钥匙", 40_000_000)
    request = dict(c.query, query_mode="COUNT", text="字幕中「接住那把钥匙」出现几次", top_k=1)
    result = c.service.search(c.ws, request)
    assert result["verification_required"] and result["truncated"] and result["candidate_count"] == 3
    c.catalog.checked = {c.mid: [dict(start_us=10_000_000, end_us=3_600_000_000)]}
    answer = c.service.answer(c.ws, uid(), dict(request, query_mode="FIRST"))
    validate_contract("AnswerResult", answer)
    assert answer["found_event_count"] == 3 and answer["count_qualifier"] == "OBSERVED_ONLY"
    assert "目前" in answer["answer"] and "首次匹配" not in answer["answer"]
    assert answer["coverage_summary"]["unverified_ranges"] == [
        dict(media_revision_id=c.mid, range=dict(start_us=0, end_us=10_000_000))
    ]
    c.catalog.checked[c.mid] = [dict(start_us=0, end_us=41_000_000)]
    prefix_answer = c.service.answer(c.ws, uid(), dict(request, query_mode="FIRST"))
    assert "首次匹配" in prefix_answer["answer"]
    assert prefix_answer["count_qualifier"] == "OBSERVED_ONLY"
    c.catalog.checked[c.mid] = [dict(start_us=10_000_000, end_us=3_600_000_000)]
    c.catalog.checked[c.mid].append(dict(start_us=0, end_us=10_000_000))
    answer = c.service.answer(c.ws, uid(), request)
    assert answer["found_event_count"] == 3 and answer["count_qualifier"] == "WITHIN_PROTOCOL"
    record = c.service.files.get(c.ws, answer["complete_event_list_artifact_id"])
    payload = json.loads((c.service.files.root / c.ws / record.digest).read_text(encoding="utf8"))
    assert len(payload["events"]) == 3 and len(payload["events"][0]["evidence_ids"]) == 2
    assert answer["coverage_summary"]["semantic_exhaustiveness_guaranteed"] is False
    app = FastAPI()
    app.state.edit_sources = c.store
    app.state.processing = SimpleNamespace(catalog=c.catalog)
    install_search_routes(app, lambda: {"id": 1})
    with TestClient(app) as http:
        path = f"/v1/projects/{c.project}/query-results/{record.id}/occurrences"
        page = http.get(path, params={"limit": 2}).json()
        rest = http.get(path, params={"limit": 2, "cursor": page["next_cursor"]}).json()
        assert len(page["items"]) == 2 and len(rest["items"]) == 1 and rest["next_cursor"] is None
        assert http.get(path, params={"cursor": uid()}).status_code == 422
        wrong = c.store.create_project(c.ws, "不同项目")["project_id"]
        assert http.get(path.replace(c.project, wrong)).status_code == 404


def test_incomplete_encoder_response_preserves_lexical_hits(ctx, monkeypatch):
    from http.client import IncompleteRead
    from dovideo.infrastructure.search_encoder import SearchEncoder

    c = ctx
    encoder = SearchEncoder("http://localhost:8174", c.folder)

    def incomplete(*args, **kwargs):
        raise IncompleteRead(b"[", 16)

    monkeypatch.setattr(encoder.http, "open", incomplete)
    c.service.encoder = encoder
    row = c.evidence("接住那把钥匙", 1)
    row["source_generation_id"] = c.catalog.gen
    result = c.service.search(c.ws, c.query)
    assert result["hits"][0]["evidence_ids"] == [row["evidence_id"]]
    assert any("编码器不可用" in msg for msg in result["warnings"])


def test_artifacts_restore_on_another_cache_and_recheck_scope(ctx):
    from pathlib import Path
    from uuid import UUID, uuid5
    from dovideo.infrastructure.object_sources import SourceObjects
    from dovideo.infrastructure.search_artifacts import SearchArtifacts

    c = ctx
    remote = {}

    class Bucket:
        def fput_object(self, bucket, key, path):
            remote[key] = Path(path).read_bytes()

        def fget_object(self, bucket, key, path):
            Path(path).write_bytes(remote[key])

    writer = c.service.files
    c.store.objects = SourceObjects(Bucket(), "scratch")
    parent = writer.save(c.ws, c.project, b"an image", "IMAGE_QUERY", "image/png")
    metadata = dict(parent_artifact_id=parent, roi=dict(x=2, y=3, width=7, height=4))
    image = writer.save(c.ws, c.project, b"cropped pixels", "IMAGE_QUERY", "image/png")
    writer.save(
        c.ws,
        c.project,
        json.dumps(metadata).encode(),
        "QUERY_IMAGE_META",
        "application/json",
        ident=str(uuid5(UUID(image), "roi-provenance")),
    )
    key = writer.save(
        c.ws, c.project, b'{"events":[{"id":"event-1"}]}', "QUERY_RESULT", "application/json", [c.mid]
    )
    proof = writer.save(
        c.ws,
        c.project,
        b'{"evidence":{"support":"SUPPORTED"}}',
        "QUERY_EVIDENCE",
        "application/json",
        [c.mid],
    )
    replica = EditSources(c.store.sessions.kw["bind"], c.folder / "node-b")
    replica.objects = c.store.objects
    restored = SearchArtifacts(replica)
    assert not restored.root.exists()
    assert restored.image(c.ws, c.project, image) == b"cropped pixels"
    assert restored.roi(c.ws, c.project, image) == metadata
    assert restored.occurrences(c.ws, c.project, key) == [{"id": "event-1"}]
    assert restored.verified(c.ws, proof) == {"support": "SUPPORTED"}
    with c.store.sessions.begin() as db:
        db.execute(sources.update().where(sources.c.revision == c.mid).values(probe={"deleted": True}))
    with pytest.raises(HTTPException) as exc:
        restored.occurrences(c.ws, c.project, key)
    assert exc.value.status_code == 404


@pytest.mark.parametrize("server", [False, True], ids=["local-files", "http-qdrant"])
def test_retention_removes_only_tombstoned_search_data(ctx, server, monkeypatch):
    import os, time
    from sqlalchemy import select
    from dovideo.infrastructure.persistence.processing_tables import records, runs as processing
    from dovideo.infrastructure.search_cleanup import cleanup_search
    from dovideo.infrastructure.search_encoder import SearchEncoder
    from dovideo.infrastructure.vector.evidence_index import EvidenceIndex, point

    url = os.getenv("VIDEOMIND_TEST_SEARCH_GC_QDRANT_URL")
    if server and not url:
        pytest.skip("requires an isolated HTTP Qdrant test server")
    for name in ("VIDEOMIND_SEARCH_QDRANT_PATH", "VIDEOMIND_SEARCH_QDRANT_URL"):
        monkeypatch.delenv(name, raising=False)
    c = ctx
    db = c.store.sessions.kw["bind"]
    processing.create(db)
    records.create(db)
    index = EvidenceIndex(url=url) if server else None
    minio_url = os.getenv("VIDEOMIND_TEST_SEARCH_GC_MINIO")
    if server and minio_url:
        from minio import Minio
        from dovideo.infrastructure.object_sources import SourceObjects

        client = Minio(
            minio_url,
            access_key=os.environ["MINIO_ROOT_USER"],
            secret_key=os.environ["MINIO_ROOT_PASSWORD"],
            secure=False,
        )
        bucket = "gc-" + uid()
        client.make_bucket(bucket)
        c.store.objects = SourceObjects(client, bucket)
    model = dict(version="retention-test-" + uid(), dimensions=3)
    encoder = SearchEncoder("", c.store.root / "embeddings", index=index)
    other_ws = c.store.workspace({"id": 2})
    other_project = c.store.create_project(other_ws, "foreign")["project_id"]
    active, foreign = c.media(), c.media(other_ws, other_project)
    file_store = c.service.files
    raw = file_store.save(c.ws, c.project, b"same bytes", "IMAGE_QUERY", "image/png")
    duplicate = file_store.save(c.ws, c.project, b"same bytes", "QUERY_RESULT", "application/json", [c.mid])
    secret = file_store.save(c.ws, c.project, b"deleted result", "QUERY_RESULT", "application/json", [c.mid])
    protected = file_store.save(
        other_ws, other_project, b"deleted result", "QUERY_RESULT", "application/json", [foreign]
    )
    secret_path = file_store.path(file_store.get(c.ws, secret))
    scan = file_store.freeze(
        c.ws, uid(), "q", dict(project=c.project, requested=[dict(media_revision_id=c.mid)])
    )
    prepared = []
    for scope, pid, mid, claim in (
        (c.ws, c.project, c.mid, "secret"),
        (c.ws, c.project, active, "shared"),
        (other_ws, other_project, foreign, "secret"),
    ):
        component = uid()
        row = c.evidence(claim, 100, mid=mid, scope=scope, pid=pid)
        row["source_generation_id"] = component
        with db.begin() as con:
            asset = con.execute(select(sources.c.id).where(sources.c.revision == mid)).scalar_one()
            con.execute(
                processing.insert().values(
                    id=component,
                    workspace=scope,
                    project=pid,
                    revision=mid,
                    asset=asset,
                    kind="INDEX",
                    config={},
                    status="SUCCEEDED",
                    stage="PUBLISHED",
                    completed=1,
                    attempt=1,
                    lease_end=0,
                    created="2026-11-01",
                )
            )
            con.execute(
                records.insert().values(
                    id=uid(),
                    workspace=scope,
                    asset=asset,
                    run=component,
                    kind="EVIDENCE",
                    unit="cue",
                    start_us=100,
                    body=dict(evidence=row, source="embedded-subtitle"),
                )
            )
        encoder.freeze_spaces(scope, [row], {"text": model})
        vector = encoder.vector_path(scope, model, claim)
        vector.parent.mkdir(parents=True, exist_ok=True)
        vector.write_bytes(b"vector")
        if index:
            index.write(model, [point(row, [1, 0, 0])])
        prepared.append((row, vector))
    with db.begin() as con:
        con.execute(
            sources.update().where(sources.c.revision == c.mid).values(probe=dict(deleted=True, deleted_at=1))
        )
    cutoff = time.time() + 1
    assert cleanup_search(c.store, cutoff=cutoff, index=index) and secret_path.is_file()
    removed = cleanup_search(c.store, cutoff=cutoff, index=index, apply=True)
    assert "query-scan/" + scan.run_id in removed
    assert (
        file_store.get(c.ws, secret) is None
        and file_store.get(c.ws, duplicate) is None
        and not secret_path.exists()
    )
    assert (
        file_store.image(c.ws, c.project, raw) == b"same bytes"
        and file_store.path(file_store.get(other_ws, protected)).is_file()
    )
    assert not prepared[0][1].exists() and all(p.is_file() for _, p in prepared[1:])
    if c.store.objects:
        objects = {item.object_name for item in client.list_objects(bucket, recursive=True)}
        assert file_store.object_key(c.ws, secret_path.name) not in objects
        assert file_store.object_key(other_ws, secret_path.name) in objects
        assert file_store.object_key(c.ws, file_store.get(c.ws, raw).digest) in objects
    if index:
        assert index.rank(model, [1, 0, 0], [prepared[0][0]], 1) == []
        assert all(index.rank(model, [1, 0, 0], [row], 1) for row, _ in prepared[1:])
    assert cleanup_search(c.store, cutoff=cutoff, index=index, apply=True) == []
    with pytest.raises(HTTPException):
        file_store.save(c.ws, c.project, b"later publication", "QUERY_EVIDENCE", "application/json", [c.mid])


def test_unchecked_action_is_not_confirmed_by_frame(ctx):
    c = ctx
    c.evidence("人物拿着杯子", 2_000_000, source="sampled-frame")
    c.evidence("人物拿着杯子", 2_000_000, source="tesseract")
    answer = c.service.answer(c.ws, uid(), dict(c.query, text="拿起杯子再转身", query_mode="ALL"))
    assert answer["found_event_count"] == 0 and answer["coverage_summary"]["status"] == "PARTIAL"
    assert all(claim["support"] == "UNCERTAIN" for claim in answer["claims"])


def test_tombstone_during_encoding_prevents_late_publication(ctx, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from dovideo.infrastructure.search_encoder import SearchEncoder

    c = ctx
    encoder = SearchEncoder("http://localhost:8174", c.folder / "vectors")
    service = EvidenceSearch(c.store, c.catalog, encoder)
    row = c.evidence("deleted during slow inference", 10)
    row["source_generation_id"] = uid()
    model = {"version": "test-checkpoint", "dimensions": 2}
    started, released = Event(), Event()

    def exchange(path, body=None):
        if path == "/spaces":
            return {"text": model}
        started.set()
        assert released.wait(5)
        return {"version": model["version"], "vectors": [[1, 0]]}

    monkeypatch.setattr(encoder, "call", exchange)
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(service.prepare_rows, c.ws, [row])
        assert started.wait(5)
        with c.store.sessions.begin() as db:
            db.execute(
                sources.update()
                .where(sources.c.revision == c.mid)
                .values(probe={"deleted": True, "deleted_at": 1})
            )
        released.set()
        with pytest.raises(HTTPException):
            future.result(5)
    assert not encoder.vector_path(c.ws, model, row["claim"]).is_file()
    assert not encoder.generation_path(c.ws, row["source_generation_id"]).exists()


def test_completed_scan_replays_immutable_artifact_and_rechecks_source_acl(ctx):
    c = ctx
    c.evidence("给我三分钟", 10_000_000)
    run = uid()
    query = dict(c.query, text="字幕：「给我三分钟」", query_mode="ALL")
    result = c.service.answer(c.ws, run, query)
    c.catalog.gen = uid()
    c.catalog.rows.clear()
    assert c.service.answer(c.ws, run, query) == result
    with pytest.raises(HTTPException) as changed:
        c.service.answer(c.ws, run, dict(query, text="另一个问题"))
    assert changed.value.status_code == 409
    with c.store.sessions.begin() as db:
        db.execute(sources.update().where(sources.c.revision == c.mid).values(probe={"deleted": True}))
    with pytest.raises(HTTPException) as deleted:
        c.service.answer(c.ws, run, query)
    assert deleted.value.status_code == 404
    with pytest.raises(HTTPException):
        c.service.files.occurrences(c.ws, c.project, result["complete_event_list_artifact_id"])


def test_point_evidence_hits_use_the_intersecting_playable_segment(ctx):
    from dovideo.application.evidence_search import playback_range

    t = 12_833_333
    row = {"range": {"start_us": t, "end_us": t + 1}, "frame": {"actual_us": t}}
    assert playback_range(row, {"start_us": 0, "end_us": 15_000_000}) == {
        "start_us": 8_000_000,
        "end_us": 15_000_000,
    }
    assert playback_range(row, {"start_us": 11_000_000, "end_us": 14_000_000}) == {
        "start_us": 11_000_000,
        "end_us": 14_000_000,
    }
    assert row["range"] == {"start_us": t, "end_us": t + 1}


def test_delete_during_retrieval_cannot_publish_the_stale_hit(ctx, monkeypatch):
    c = ctx
    c.evidence(c.query["text"], 0)
    rank = c.service.rank

    def delete_during_rank(*args):
        result = rank(*args)
        with c.store.sessions.begin() as db:
            db.execute(sources.update().where(sources.c.revision == c.mid).values(probe={"deleted": True}))
        return result

    monkeypatch.setattr(c.service, "rank", delete_during_rank)
    with pytest.raises(HTTPException) as denied:
        c.service.search(c.ws, c.query)
    assert denied.value.status_code == 404


def test_interrupted_scan_resumes_original_generation_and_retains_conflict(ctx, monkeypatch):
    c = ctx
    row = c.evidence("字幕中说给我三分钟", 10_000_000)
    first_generation = c.catalog.gen
    run = uid()
    request = dict(c.query, text="字幕：「给我三分钟」", query_mode="ALL")
    real = c.service.ground

    def unavailable(*args):
        raise InterruptedError("worker restart")

    monkeypatch.setattr(c.service, "ground", unavailable)
    with pytest.raises(InterruptedError):
        c.service.answer(c.ws, run, request)
    c.catalog.history[first_generation] = c.catalog.rows[:]
    c.catalog.gen = uid()
    c.catalog.rows.clear()
    monkeypatch.setattr(c.service, "ground", real)
    answer = c.service.answer(c.ws, run, request)
    assert answer["found_event_count"] == 1
    assert row["evidence_id"] in answer["claims"][0]["evidence_ids"]
    c.evidence("给我三分钟", 15_000_000, source="tesseract")["support"] = "CONFLICTING"
    report = c.service.answer(c.ws, uid(), dict(c.query, text="给我三分钟"))
    assert report["claims"][0]["support"] == "CONFLICTING"


def test_real_cropped_jpeg_query_and_scoped_artifact(ctx):
    c = ctx
    img = Image.new("RGB", (160, 96), "navy")
    draw = ImageDraw.Draw(img)
    draw.ellipse((2, 3, 62, 90), fill="orange")
    draw.polygon([(155, 8), (100, 55), (130, 92)], fill="white")
    for name, frame in (("target", img), ("other", Image.new("RGB", img.size, "red"))):
        path = c.folder / (name + ".png")
        frame.save(path)
        row = c.evidence(
            "已抽帧",
            20 if name == "target" else 40,
            source="sampled-frame",
            frame=dict(sha256=name, actual_us=20 if name == "target" else 40, artifact_id=uid()),
            path=path,
        )
        if name == "target":
            target = row
    query = BytesIO()
    img.crop((20, 12, 140, 84)).resize((80, 48)).save(query, "JPEG", quality=55)
    app = FastAPI()
    app.state.edit_sources = c.store
    app.state.processing = SimpleNamespace(catalog=c.catalog)
    install_search_routes(app, lambda: {"id": 1})
    with TestClient(app) as http:
        url = f"/v1/projects/{c.project}/query-images"
        assert http.post(url, files={"file": ("fake.jpg", b"not an image", "image/jpeg")}).status_code == 422
        upload = http.post(url, files={"file": ("jpeg.txt", query.getvalue(), "text/plain")})
        assert upload.status_code == 201, upload.text
        artifact = upload.json()
        validate_contract("Artifact", artifact)
        assert artifact["mime_type"] == "image/jpeg" and http.get(artifact["url"]).content == query.getvalue()
        request = dict(
            scope=c.query["scope"],
            image_artifact_id=artifact["artifact_id"],
            query_mode="LOCAL",
            top_k=1,
        )
        result = http.post("/v1/search", json=request)
        assert result.status_code == 200, result.text
        hit = result.json()["hits"][0]
        assert hit["range"] == target["range"] and hit["route_ranks"][0]["route"] == "image_spatial"
        assert not hit["verified"]
        other = c.store.create_project(c.ws, "同账号另一个项目")["project_id"]
        assert http.post("/v1/search", json=dict(request, scope={"project_id": other})).status_code == 404
        unchanged = http.post(
            url,
            files={
                "file": (
                    "same.png",
                    (c.folder / "target.png").read_bytes(),
                    "image/png",
                )
            },
        ).json()
        answer = app.state.evidence_search.answer(
            c.ws, uid(), dict(request, image_artifact_id=unchanged["artifact_id"])
        )
        proof = app.state.evidence_search.verified_evidence(c.ws, answer["claims"][0]["evidence_ids"][0])
        validate_contract("Evidence", proof)
        assert proof["support"] == "SUPPORTED" and proof["artifact_ids"] == [
            unchanged["artifact_id"],
            target["frame"]["artifact_id"],
        ]
        assert (
            proof["range"]["start_us"] == target["frame"]["actual_us"]
            and proof["index_generation_id"] is None
        )
        assert app.state.evidence_search.verified_evidence(uid(), proof["evidence_id"]) is None
        roi_url = f"{url}/{unchanged['artifact_id']}/roi"
        box = dict(x=20, y=12, width=120, height=72)
        assert http.post(roi_url, json=dict(box, width=999)).status_code == 422
        cropped = http.post(roi_url, json=box).json()
        assert http.post(roi_url, json=box).json()["artifact_id"] == cropped["artifact_id"]
        region = app.state.evidence_search.answer(
            c.ws, uid(), dict(request, image_artifact_id=cropped["artifact_id"])
        )
        region_proof = app.state.evidence_search.verified_evidence(
            c.ws, region["claims"][0]["evidence_ids"][0]
        )
        assert unchanged["artifact_id"] in region_proof["artifact_ids"] and "选区" in region_proof["claim"]
        near = app.state.evidence_search.answer(c.ws, uid(), request)
        assert all(c["support"] == "UNCERTAIN" for c in near["claims"])
