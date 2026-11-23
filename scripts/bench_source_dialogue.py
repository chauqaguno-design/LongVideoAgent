"""Check BGE and hybrid ranking against the real CineTrace development ASR observations."""

import argparse
import json
from pathlib import Path
from time import perf_counter

from sqlalchemy import create_engine

from dovideo.application.edit_sources import EditSources
from dovideo.application.evidence_search import EvidenceSearch
from dovideo.infrastructure.persistence.processing_catalog import MediaCatalog
from dovideo.infrastructure.search_encoder import SearchEncoder
from dovideo.infrastructure.vector.evidence_index import EvidenceIndex


CASES = [
    ("zh", "有人请对方稍等片刻", 8_900_000),
    ("en", "Someone praises a brilliant newspaperman", 10_060_000),
    ("mixed", "谁提到了 John Doe 的人生", 2_760_000),
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("processing_folder", type=Path)
    parser.add_argument("--encoder", default="http://127.0.0.1:18176")
    parser.add_argument("--qdrant-url", help="Use a dedicated test server for the evidence collections")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.processing_folder
    receipt = json.loads((root / "verification.json").read_text(encoding="utf8"))
    asset = receipt["asset"]
    expected = "52bf9b5d3bd71f7914dcab6ea99b4a68a9b13543334736ac098ad64d25d3bf79"
    if asset["sha256"] != expected:
        parser.error("The reference time labels belong to CineTrace-15s.mp4")
    db = create_engine(f"sqlite:///{root / 'results.sqlite3'}")
    store = EditSources(db, root)
    catalog = MediaCatalog(store)
    ws, pid, mid = (asset[k] for k in ("workspace_id", "project_id", "media_revision_id"))
    generation, rows = catalog.search_scope(ws, pid, [mid])
    by_id = {r["evidence_id"]: r for r in rows}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    index = EvidenceIndex(url=args.qdrant_url) if args.qdrant_url else None
    encoder = SearchEncoder(args.encoder, args.output.parent / "dialogue-cache", index=index)
    search = EvidenceSearch(store, catalog, encoder)
    spaces = encoder.prepare(ws, rows)
    cases = []
    for language, query, expected_us in CASES:
        start = perf_counter()
        result = search.search(
            ws,
            {
                "scope": {"project_id": pid, "media_revision_ids": [mid]},
                "text": query,
                "query_mode": "LOCAL",
                "top_k": 20,
            },
        )
        elapsed = perf_counter() - start
        dense = []
        for hit in result["hits"]:
            for route in hit["route_ranks"]:
                if route["route"] in {"text_dense", "text_qdrant"}:
                    dense.append((route["rank"], hit, route["raw_score"]))
        dense.sort(key=lambda item: item[0])

        def track(hit):
            row = by_id[hit["evidence_ids"][0]]
            return {
                "source": row["source"],
                "evidence_id": row["evidence_id"],
                "start_us": row["range"]["start_us"],
            }

        best = track(dense[0][1])
        cases.append(
            dict(
                language=language,
                query=query,
                expected_asr_start_us=expected_us,
                bge_top=best,
                bge_top1=best["source"] == "faster-whisper" and best["start_us"] == expected_us,
                bge_score=dense[0][2],
                hybrid_top=track(result["hits"][0]),
                seconds=elapsed,
                warnings=result["warnings"],
            )
        )
    report = dict(
        scope="Three non-blind development queries against the sample's actual ASR observations",
        generation=generation,
        source_sha256=expected,
        observation_count=len(rows),
        spaces=spaces,
        bge_top1=sum(c["bge_top1"] for c in cases),
        cases=cases,
        api_tokens=0,
        api_cost=0,
    )
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf8")
    print(f"{report['bge_top1']}/{len(cases)} BGE top-one dialogue timestamps")
    search.close()
    db.dispose()


if __name__ == "__main__":
    main()
