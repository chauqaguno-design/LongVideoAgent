"""Rank transformed screenshots against nearby frames in the 15-second CineTrace development sample."""
import argparse
from bisect import bisect_right
from fractions import Fraction
from hashlib import sha256
from io import BytesIO
from itertools import batched
import json
from math import hypot
from pathlib import Path
import subprocess
from time import perf_counter

from PIL import Image

from dovideo.infrastructure.search_encoder import SearchEncoder, image_input
from dovideo.presentation.composition import AnalysisSettings


SOURCE_SHA = "52bf9b5d3bd71f7914dcab6ea99b4a68a9b13543334736ac098ad64d25d3bf79"
# First decoded frame of each shot at FFmpeg scene score > 0.25.
SHOT_STARTS = (0, 103, 219, 388)
TIE_EPSILON = 1e-6


def screenshots(image):
    w, h = image.size
    crop = image.crop((w // 8, h // 8, 7 * w // 8, 7 * h // 8))
    yield "center_crop_75", crop
    resized = crop.resize((120, 90), Image.Resampling.LANCZOS)
    for name, pixels in (("jpeg_q25", image), ("crop_resize_jpeg_q25", resized)):
        data = BytesIO()
        pixels.save(data, format="JPEG", quality=25)
        yield name, Image.open(BytesIO(data.getvalue())).convert("RGB")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--encoder", default="http://127.0.0.1:18176")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if sha256(args.source.read_bytes()).hexdigest() != SOURCE_SHA:
        parser.error("These shot boundaries refer to the CineTrace-15s development sample.")
    media = AnalysisSettings.from_environment()
    probe = json.loads(subprocess.check_output([
        media.ffprobe_executable, "-v", "error", "-select_streams", "v:0", "-show_frames", "-show_streams",
        "-show_entries", "frame=best_effort_timestamp:stream=time_base,start_pts", "-of", "json", str(args.source),
    ]))
    stream = probe["streams"][0]
    pts = [f["best_effort_timestamp"] for f in probe["frames"]]
    usec = [round((p - stream["start_pts"]) * Fraction(stream["time_base"]) * 1_000_000) for p in pts]
    centers = [(start + end - 1) // 2 for start, end in zip(SHOT_STARTS, (*SHOT_STARTS[1:], len(pts)))]
    numbers = [n + off for n in centers for off in (-8, -1, 0, 1, 8)]
    frames = {}
    for n in numbers:
        png = subprocess.check_output([
            media.ffmpeg_executable, "-nostdin", "-v", "error", "-i", str(args.source), "-vf", f"select=eq(n\\,{n})",
            "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1",
        ])
        frames[n] = Image.open(BytesIO(png)).convert("RGB")
    client = SearchEncoder(args.encoder, args.output.parent)
    model = client.call("/spaces")["vision"]
    queries = [(n, kind, img) for n in centers for kind, img in screenshots(frames[n])]
    images = [frames[n] for n in numbers] + [img for _, _, img in queries]
    vectors = []
    started = perf_counter()
    for batch in batched(images, 2):
        for vector in client.encode("vision", model, [image_input(image) for image in batch]):
            norm = hypot(*vector)
            vectors.append([v / norm for v in vector])
    elapsed = perf_counter() - started
    cases = []
    for (target, kind, _), query in zip(queries, vectors[len(numbers):], strict=True):
        shot = bisect_right(SHOT_STARTS, target)
        ranking = []
        for n, embedding in zip(numbers, vectors[:len(numbers)], strict=True):
            shot_id = bisect_right(SHOT_STARTS, n)
            relation = "same_shot" if shot_id == shot else "other_shot"
            if n == target:
                relation = "original"
            ranking.append(dict(frame=n, actual_us=usec[n], shot=shot_id, relation=relation,
                                same_pixels=frames[n].tobytes() == frames[target].tobytes(),
                                cosine=sum(a * b for a, b in zip(query, embedding, strict=True))))
        ranking.sort(key=lambda r: (-r["cosine"], r["frame"]))
        position = next(i for i, r in enumerate(ranking, 1) if r["frame"] == target)
        score = ranking[position - 1]["cosine"]
        tie_band = [i for i, r in enumerate(ranking, 1) if abs(r["cosine"] - score) <= TIE_EPSILON]
        cases.append(dict(transform=kind, target_frame=target, actual_us=usec[target], target_shot=shot,
                          exact_rank=position, tie_ranks=[min(tie_band), max(tie_band)],
                          same_shot_first=ranking[0]["shot"] == shot, ranks=ranking))
    report = dict(scope="development sample", source_sha256=SOURCE_SHA, model=model, tie_epsilon=TIE_EPSILON,
                  scene_threshold=0.25, shot_start_frames=SHOT_STARTS, frame_offsets=[-8, -1, 0, 1, 8],
                  gallery_size=len(numbers),
                  inference_seconds=round(elapsed, 3), queries=cases)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
    print(f"Exact frame first: {sum(c['exact_rank'] == 1 for c in cases)}/{len(cases)}")
    print(f"Correct shot first: {sum(c['same_shot_first'] for c in cases)}/{len(cases)}")


if __name__ == "__main__":
    main()
