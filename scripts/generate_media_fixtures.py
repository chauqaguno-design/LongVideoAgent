"""Encode then independently decode every frame and audio sample against a declared source timeline."""

import argparse
from fractions import Fraction
from hashlib import sha256
import json
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
TRUTH = ROOT / "tests/fixtures/media/timing_truth.json"


def raster(size, index, t):
    """The source-relative clock and sequence number are part of the real picture."""
    background = tuple(25 + (index * salt) % 100 for salt in (41, 19, 7))
    image = Image.new("RGB", size, background)
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default_imagefont()
    stamp = f"00:00:{t // 1000:02}.{t % 1000:03}"
    draw.rectangle((4, 4, size[0] - 5, 38), fill="black")
    draw.text((8, 6), stamp, fill="white", font=font)
    draw.text((8, 20), f"FRAME {index:02}", fill="white", font=font)
    draw.rectangle((6 + index * 4, 48, 24 + index * 4, 64), fill=(220, 180, 32))
    return image


def run(tool, *args):
    return subprocess.check_output([tool, "-v", "error", *map(str, args)])


def generate(out, ffmpeg, ffprobe, manifest=TRUTH):
    spec = json.loads(Path(manifest).read_text(encoding="utf8"))
    out.mkdir(parents=True, exist_ok=True)
    reports = []
    for item in spec["fixtures"]:
        size = tuple(item["size"])
        stamps = item["relative_pts_ms"]
        origin = item["source_origin_ms"]
        expected_rgb = []
        source = out / (item["name"] + ".mkv")
        audio = item["audio"]
        with TemporaryDirectory(dir=out) as scratch:
            tmp = Path(scratch)
            listing = []
            for i, t in enumerate(stamps):
                picture = raster(size, i, t)
                expected_rgb.append(sha256(picture.tobytes()).hexdigest())
                filename = f"{i:03d}.png"
                picture.save(tmp / filename)
                listing.append(f"file '{filename}'")
                if i + 1 < len(stamps):
                    listing.append(f"duration {(stamps[i+1]-t)/1000:.3f}")
            playlist = tmp / "frames.ffconcat"
            playlist.write_text("\n".join(listing) + "\n", encoding="ascii")
            inputs = ["-f", "concat", "-safe", "1", "-i", playlist]
            if audio:
                tone = (
                    f"sine=frequency={audio['tone_hz']}:sample_rate={audio['rate']}:"
                    f"duration={audio['sample_count']/audio['rate']}"
                )
                inputs.extend(["-itsoffset", str(audio["offset_ms"] / 1000), "-f", "lavfi", "-i", tone])
            run(
                ffmpeg,
                "-y",
                *inputs,
                "-fps_mode",
                "vfr",
                "-enc_time_base",
                "1:1000",
                "-c:v",
                "ffv1",
                "-pix_fmt",
                "bgr0",
                "-c:a",
                "pcm_s16le",
                "-output_ts_offset",
                origin / 1000,
                source,
            )
        info = json.loads(run(ffprobe, "-show_streams", "-show_frames", "-of", "json", source))
        stream = next(s for s in info["streams"] if s["codec_type"] == "video")
        assert (stream["width"], stream["height"]) == size
        tb = Fraction(stream["time_base"])
        video_frames = [f for f in info["frames"] if f["media_type"] == "video"]
        pts = [int(f["best_effort_timestamp"]) * tb * 1000 for f in video_frames]
        assert pts == [t + origin for t in stamps], f"Unexpected source PTS in {source.name}"
        last = video_frames[-1]
        end = (
            pts[-1] + int(last["pkt_duration"] if "pkt_duration" in last else last["duration"]) * tb * 1000
        )
        assert end == origin + item["video_end_ms"], f"Unexpected video end in {source.name}"
        decoded = run(
            ffmpeg, "-i", source, "-map", "0:v:0", "-vsync", "0", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"
        )
        pixels = size[0] * size[1] * 3
        hashes = [sha256(decoded[i : i + pixels]).hexdigest() for i in range(0, len(decoded), pixels)]
        assert hashes == expected_rgb, f"Decoded pixels or burned-in clock differs in {source.name}"
        audio_frames = [f for f in info["frames"] if f["media_type"] == "audio"]
        if audio:
            audio_stream = next(s for s in info["streams"] if s["codec_type"] == "audio")
            actual_start = int(audio_frames[0]["pts"]) * Fraction(audio_stream["time_base"]) * 1000
            assert actual_start == origin + audio["offset_ms"]
            assert int(audio_stream["sample_rate"]) == audio["rate"]
            assert sum(int(f["nb_samples"]) for f in audio_frames) == audio["sample_count"]
            pcm = run(ffmpeg, "-i", source, "-map", "0:a:0", "-f", "s16le", "-")
            assert len(pcm) == audio["sample_count"] * 2 and any(pcm)
        else:
            assert not audio_frames and not any(s["codec_type"] == "audio" for s in info["streams"])
        reports.append(
            dict(
                file=source.name,
                source_pts_ms=[int(t) for t in pts],
                rgb_sha256=hashes,
                video_end_ms=int(end),
                audio=audio,
            )
        )
    result = dict(version=spec["version"], fixtures=reports)
    (out / "verification.json").write_text(json.dumps(result, indent=2), encoding="utf8")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    ffprobe = os.getenv("DOVIDEO_FFPROBE") or shutil.which("ffprobe", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    if not ffmpeg or not ffprobe:
        parser.error("Local FFmpeg and ffprobe required")
    result = generate(args.output, ffmpeg, ffprobe)
    print(f"{len(result['fixtures'])} fixtures decoded and matched to timing, pixels and audio truth")


if __name__ == "__main__":
    main()
