"""Measure the local HTTP search and job-acceptance paths on 10,000 synthetic indexed segments."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
from tempfile import TemporaryDirectory
from threading import Barrier
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from dovideo.application.edit_sources import EditSources
from dovideo.evaluation.video_grounding import percentile
from dovideo.infrastructure.persistence.processing_tables import records, runs
from dovideo.infrastructure.persistence.search_tables import artifacts, runs as scans
from dovideo.infrastructure.persistence.source_tables import projects, sources
from dovideo.presentation.api.media_processing import install_processing
from dovideo.presentation.api.runtime import LocalAuthService
from dovideo.presentation.api.search import install_search_routes


def ident(label):
    return str(uuid5(NAMESPACE_URL, "videomind-benchmark-v1:" + label))


def rss_mib():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class Memory(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                (v, ctypes.c_size_t)
                for v in (
                    "peak",
                    "current",
                    "peak_paged",
                    "paged",
                    "peak_nonpaged",
                    "nonpaged",
                    "pagefile",
                    "peak_pagefile",
                )
            ]

        memory = Memory()
        memory.cb = ctypes.sizeof(memory)
        proc = ctypes.windll.kernel32.GetCurrentProcess
        proc.restype = wintypes.HANDLE
        info = ctypes.windll.psapi.GetProcessMemoryInfo
        info.argtypes = [wintypes.HANDLE, ctypes.POINTER(Memory), wintypes.DWORD]
        if not info(proc(), ctypes.byref(memory), memory.cb):
            return None
        return round(memory.peak / 1024**2, 2)
    import resource

    unit = 1024**2 if platform.system() == "Darwin" else 1024
    return round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / unit, 2)


def seed(store, workspace, count):
    project = store.create_project(workspace, "检索基准")["project_id"]
    media, asset, run = (ident(k) for k in ("media", "asset", "run"))
    locations = ("车站", "天台", "厨房", "公路", "教室", "书房", "商店")
    verbs = ("开门", "转身", "接过车钥匙", "点亮台灯", "走下楼梯")
    utterances = ("给我三分钟", "停在前面的桥边", "先看一下屏幕", "谢谢你的帮忙", "明天再见", "现在出发")
    with store.sessions.begin() as db:
        db.execute(
            sources.insert().values(
                id=asset,
                workspace=workspace,
                project=project,
                filename="load-fixture.mp4",
                revision=media,
                upload=ident("upload"),
                artifact=ident("source"),
                size=1,
                mime="video/mp4",
                receipt="fixed",
                digest="0" * 64,
                expires=0,
                probe=dict(duration_us=count * 10_000_000, active_run=run, browser_playable=True),
            )
        )
        db.execute(
            runs.insert().values(
                id=run,
                workspace=workspace,
                project=project,
                revision=media,
                asset=asset,
                kind="INDEX",
                status="SUCCEEDED",
                stage="DONE",
                config={},
                completed=count,
                total=count,
                attempt=1,
                lease_end=0,
                created=datetime.now(timezone.utc).isoformat(),
            )
        )
        generation_hash = hashlib.sha256()
        batch = []
        for i in range(count):
            text = f"镜头 {i}，人物在{locations[i % 7]}{verbs[i // 7 % 5]}。{utterances[i // 35 % 6]}。"
            generation_hash.update(text.encode())
            eid = ident(f"cue-{i}")
            evidence = dict(
                evidence_id=eid,
                workspace_id=workspace,
                project_id=project,
                media_revision_id=media,
                range={"start_us": i * 10_000_000, "end_us": i * 10_000_000 + 3_000_000},
                claim=text,
                support="SUPPORTED",
                observation_ids=[ident(f"obs-{i}")],
                artifact_ids=[],
                verification_protocol="benchmark-fixture",
                index_generation_id=ident("fixture"),
            )
            batch.append(
                dict(
                    id=eid,
                    workspace=workspace,
                    run=run,
                    asset=asset,
                    kind="EVIDENCE",
                    unit=f"cue-{i}",
                    start_us=i * 10_000_000,
                    body=dict(
                        evidence=evidence,
                        modality="OCR",
                        source="embedded-subtitle",
                        segment_id=ident(f"segment-{i}"),
                    ),
                )
            )
            if len(batch) == 500 or i == count - 1:
                db.execute(records.insert(), batch)
                batch = []
    return project, asset, utterances, generation_hash.hexdigest()


def run(count, rounds):
    with TemporaryDirectory() as tmp:
        root = Path(tmp)
        engine = create_engine(f"sqlite:///{(root / 'bench.db').as_posix()}")
        for table in (projects, sources, runs, records, artifacts, scans):
            table.create(engine)
        store = EditSources(engine, root)
        auth = LocalAuthService()
        auth.register("benchuser", "local-benchmark-password", "Benchmark")
        token = auth.login("benchuser", "local-benchmark-password").token

        def identity(request: Request):
            return auth.require(request.headers.get("Authorization"))

        project, asset, words, digest = seed(store, store.workspace({"id": 1}), count)
        app = FastAPI()
        app.state.edit_sources = store
        install_processing(app, identity)
        install_search_routes(app, identity)
        # Benchmark freezes the deployed lexical routes; model performance is measured separately.
        app.state.evidence_search.encoder = None
        search, accepted = [], []
        with TestClient(app, headers={"Authorization": "Bearer " + token}) as client:
            warm = dict(scope={"project_id": project}, query_mode="LOCAL", text=words[0], top_k=20)
            client.post("/v1/search", json=warm).raise_for_status()
            for i in range(rounds):
                start = perf_counter()
                response = client.post("/v1/search", json=dict(warm, text=words[i % len(words)]))
                elapsed = perf_counter() - start
                response.raise_for_status()
                body = response.json()
                search.append(
                    dict(
                        seconds=elapsed,
                        query_index=i % len(words),
                        candidate_count=body["candidate_count"],
                        query_id=body["query_id"],
                        generation=body["index_generation_id"],
                    )
                )
            barrier = Barrier(10)

            def submit(i):
                barrier.wait()
                start = perf_counter()
                r = client.post(
                    f"/v1/assets/{asset}/processing-runs",
                    json=dict(idempotency_key=f"bench-accept-{i}", requested_capabilities=["ocr"]),
                )
                elapsed = perf_counter() - start
                return dict(status=r.status_code, seconds=elapsed)

            with ThreadPoolExecutor(max_workers=10) as pool:
                accepted = list(pool.map(submit, range(10)))
        engine.dispose()
    times = [x["seconds"] for x in search]
    acceptance = [x["seconds"] for x in accepted]
    return dict(
        scope="Local ASGI API, SQLite segment/cue fixtures. Job acceptance excludes worker execution.",
        dataset_sha256=digest,
        segments=count,
        rounds=rounds,
        concurrency=10,
        backend="BM25 Chinese/English + verbatim + RRF60",
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        platform=platform.platform(),
        python=platform.python_version(),
        logical_cpus=os.cpu_count(),
        peak_rss_mib=rss_mib(),
        tokens=0,
        model_cost=0,
        search_p50_s=percentile(times, 0.5),
        search_p95_s=percentile(times, 0.95),
        accept_p50_s=percentile(acceptance, 0.5),
        accept_p95_s=percentile(acceptance, 0.95),
        accept_error_rate=sum(x["status"] != 202 for x in accepted) / 10,
        queue_wait_s=None,
        execution_s=None,
        trials=search,
        admissions=accepted,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rounds", type=int, default=30)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run(10_000, args.rounds)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print({k: v for k, v in report.items() if k not in {"trials", "admissions"}})


if __name__ == "__main__":
    main()
