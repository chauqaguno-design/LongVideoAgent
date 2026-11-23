"""Measure local source-frame retrieval on three deterministic crops of a real video."""

import argparse
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import subprocess
from time import perf_counter
from uuid import NAMESPACE_URL, uuid5

from PIL import Image
from dovideo.evaluation.video_grounding import GROUPS, evaluate
from dovideo.infrastructure.search_encoder import SearchEncoder
from dovideo.infrastructure.search_features import descriptor, frame_descriptors, similarity
from dovideo.infrastructure.vector.evidence_index import EvidenceIndex
from dovideo.presentation.composition import AnalysisSettings


def write(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--encoder", default="http://127.0.0.1:18174")
    parser.add_argument("--output", type=Path, required=True)
    backend = parser.add_mutually_exclusive_group()
    backend.add_argument(
        "--qdrant-dir", type=Path, help="Use an isolated local Qdrant collection for this run"
    )
    backend.add_argument("--qdrant-url", help="Use the HTTP API on a dedicated test server")
    args = parser.parse_args()
    out = args.output
    out.mkdir(parents=True, exist_ok=True)
    bins = AnalysisSettings.from_environment()
    info = json.loads(
        subprocess.check_output(
            [
                bins.ffprobe_executable,
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_frames",
                "-show_streams",
                "-show_entries",
                "format=duration:stream=time_base,start_pts:frame=best_effort_timestamp",
                "-of",
                "json",
                str(args.source),
            ]
        )
    )
    stream = info["streams"][0]
    base = Fraction(stream["time_base"])
    duration = round(Fraction(info["format"]["duration"]) * 1_000_000)
    rows = []
    for j, fraction in enumerate((1, 3, 6)):
        n = fraction * len(info["frames"]) // 7
        pts = info["frames"][n]["best_effort_timestamp"]
        actual = round((pts - stream.get("start_pts", 0)) * base * 1_000_000)
        path = out / f"source-{j}.jpg"
        subprocess.run(
            [
                bins.ffmpeg_executable,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(args.source),
                "-vf",
                f"select=eq(n\\,{n}),scale=384:-2",
                "-frames:v",
                "1",
                str(path),
            ],
            check=True,
        )
        ident = str(
            uuid5(NAMESPACE_URL, f"videomind-dev-anchor:{sha256(path.read_bytes()).hexdigest()}:{actual}")
        )
        rows.append(
            dict(
                workspace_id="local-fixture",
                project_id="dev-project",
                media_revision_id="dev-clip",
                source_generation_id="three-anchors-v1",
                evidence_id=ident,
                segment_id=ident,
                range=dict(start_us=actual, end_us=actual + 1),
                source="sampled-frame",
                path=str(path),
                frame={"actual_us": actual, "sha256": sha256(path.read_bytes()).hexdigest()},
            )
        )
    index = (
        EvidenceIndex(path=args.qdrant_dir, url=args.qdrant_url)
        if args.qdrant_dir or args.qdrant_url
        else None
    )
    encoder = SearchEncoder(args.encoder, out / "cache", index=index)
    start = perf_counter()
    spaces = encoder.prepare("local-fixture", rows)
    build = perf_counter() - start
    with args.source.open("rb") as file:
        import hashlib

        source_sha = hashlib.file_digest(file, "sha256").hexdigest()
    manifest = dict(
        split="DEV",
        media=[
            dict(
                id="clip",
                sha256=source_sha,
                origin_id=source_sha,
                category="story",
                duration_us=duration,
                license="user-supplied private development sample",
            )
        ],
        queries=[],
    )
    predictions = dict(
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        model_versions=spaces,
        generation="three-known-source-anchors-v1",
        config_digest=sha256(b"crops=center.75;size=142x100;jpeg=60;anchors=1,3,6/7").hexdigest(),
        queries=[],
    )
    reports = []
    for i, row in enumerate(rows):
        with Image.open(row["path"]) as frame:
            w, h = frame.size
            cut = frame.crop((w // 8, h // 8, w * 7 // 8, h * 7 // 8))
            cut.thumbnail((142, 100))
            data = BytesIO()
            cut.save(data, "JPEG", quality=60)
            query = Image.open(BytesIO(data.getvalue())).convert("RGB")
        ranks, warnings = {}, []
        start = perf_counter()
        encoder.rank("", query, rows, ranks, warnings)
        elapsed = perf_counter() - start
        route = "vision_qdrant" if index else "vision_dense"
        if route not in ranks:
            raise RuntimeError(warnings)
        scores = ranks[route][1]
        pixels = sorted(
            range(len(rows)),
            key=lambda j: -similarity(
                descriptor(query), frame_descriptors(rows[j]["path"], rows[j]["frame"]["sha256"])
            ),
        )
        actual = row["frame"]["actual_us"]
        label = f"crop-{i}"
        gt = dict(
            id=label,
            media="clip",
            start_us=actual,
            end_us=actual,
            boundary="FRAME",
            basis=f"source pts {actual}us",
        )
        manifest["queries"].append(
            dict(
                id=label,
                type="IMAGE",
                scope=["clip"],
                query=sha256(data.getvalue()).hexdigest(),
                event_definition="the true original frame for this mechanical crop",
                annotator="FFmpeg source extraction",
                reviewer="deterministic PTS cross-check",
                groups=dict.fromkeys(GROUPS, "mechanical-crop"),
                exhaustive=False,
                events=[gt],
            )
        )
        candidates = [dict(media="clip", actual_us=rows[c.index]["frame"]["actual_us"]) for c in scores]
        predictions["queries"].append(
            dict(id=label, candidates=candidates, grounded=[], claims=[], answered=False, tenant_leaks=0)
        )
        reports.append(
            dict(
                expected_actual_us=actual,
                dense_actual_us=candidates[0]["actual_us"],
                dense_top1=scores[0].index == i,
                pixel_top1=pixels[0] == i,
                dense_similarity=scores[0].score,
                seconds=elapsed,
            )
        )
    write(out / "development-manifest.json", manifest)
    write(out / "development-predictions.json", predictions)
    write(out / "development-metrics.json", evaluate(manifest, predictions))
    write(
        out / "screenshot-smoke.json",
        dict(
            scope="development fixture, not blind benchmark",
            route=route,
            source_sha256=source_sha,
            encoder=spaces,
            source_anchor_count=len(rows),
            build_seconds=build,
            queries=reports,
        ),
    )
    if index:
        index.close()
    print(f"{sum(r['dense_top1'] for r in reports)}/{len(rows)} exact top-one source anchors")


if __name__ == "__main__":
    main()
