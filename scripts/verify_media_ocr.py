"""Check a small translated screen caption against conflicting speech through the real worker."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw, ImageFont


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spoken_wav", type=Path, help="Recording of --phrase")
    parser.add_argument("font", type=Path, help="Font containing Chinese glyphs")
    parser.add_argument("output", type=Path)
    parser.add_argument("--phrase", default="Open the yellow door")
    parser.add_argument("--caption", default="请把钥匙放在桌上")
    parser.add_argument("--font-size", type=int, default=16)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    image = Image.new("RGB", (640, 360), (132, 134, 136))
    font = ImageFont.truetype(str(args.font), args.font_size)
    ImageDraw.Draw(image).text((320, 300), args.caption, fill=(84, 86, 88), font=font, anchor="mm")
    frame = args.output / "frame.png"
    source = args.output / "bilingual.mp4"
    image.save(frame)
    ffmpeg = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    subprocess.run(
        [
            ffmpeg,
            "-v",
            "error",
            "-y",
            "-loop",
            "1",
            "-framerate",
            "8",
            "-i",
            str(frame),
            "-i",
            str(args.spoken_wav),
            "-t",
            "3",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    environment = dict(os.environ, DOVIDEO_MAX_MEDIA_FRAMES="4")
    for key in (
        "VIDEOMIND_SEARCH_ENCODER_URL",
        "DOVIDEO_MEDIA_AUTO_VISUAL",
        "DOVIDEO_MEDIA_REMOTE_FLASH",
    ):
        environment.pop(key, None)
    worker = args.output / "worker"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("verify_local_processing.py")),
            str(source),
            str(worker),
        ],
        check=True,
        env=environment,
    )
    rows = json.loads((worker / "verification.json").read_text(encoding="utf8"))["observations"]
    speech = [r for r in rows if r["modality"] == "ASR" and args.phrase.casefold() in r["text"].casefold()]
    screen = [r for r in rows if r["modality"] == "OCR" and args.caption in r["text"]]
    passed = bool(
        speech and screen and set(speech[0]["artifact_ids"]).isdisjoint(screen[0]["artifact_ids"])
    )
    result = dict(
        expected_audio=args.phrase,
        expected_screen=args.caption,
        screen_font_px=args.font_size,
        background_rgb=[132, 134, 136],
        text_rgb=[84, 86, 88],
        source_size=list(image.size),
        observations=[
            {k: r[k] for k in ("modality", "text", "range", "support", "artifact_ids")} for r in rows
        ],
        passed=passed,
        external_provider_calls=0,
    )
    (args.output / "quality.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(f"{args.font_size}px; " + ", ".join(f"{r['modality']} {r['text']}" for r in rows))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
