"""Place a known spoken phrase twice over music, then check actual word evidence from the local worker."""

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spoken_wav", type=Path, help="Clean utterance matching --phrase")
    parser.add_argument("output", type=Path)
    parser.add_argument("--phrase", default="Open the yellow door")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    exe = os.getenv("DOVIDEO_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("DOVIDEO_FFMPEG_DIR"))
    source = args.output / "stereo-gold.mkv"
    bed = "aevalsrc=0.07*sin(2*PI*(220+55*mod(floor(t*2)\\,3))*t)+0.025*sin(2*PI*440*t):s=16000:d=65"
    graph = (
        "[1:a]aformat=sample_rates=16000:channel_layouts=mono,asplit=2[one][two];"
        "[one]adelay=1000[first];[two]adelay=59000[second];"
        "[first][second]amix=normalize=0,apad=whole_dur=65[left];"
        "[left][2:a]join=inputs=2:channel_layout=stereo[a]"
    )
    subprocess.run(
        [
            exe,
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=192x108:r=2:d=65",
            "-i",
            str(args.spoken_wav),
            "-f",
            "lavfi",
            "-i",
            bed,
            "-filter_complex",
            graph,
            "-map",
            "0:v",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-c:a",
            "pcm_s16le",
            str(source),
        ],
        check=True,
    )
    environment = dict(os.environ, DOVIDEO_MAX_MEDIA_FRAMES="8")
    for key in (
        "DOVIDEO_PADDLE_OCR_DIR",
        "VIDEOMIND_SEARCH_ENCODER_URL",
        "DOVIDEO_MEDIA_AUTO_VISUAL",
        "DOVIDEO_MEDIA_REMOTE_FLASH",
    ):
        environment.pop(key, None)
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("verify_local_processing.py")),
            str(source),
            str(args.output / "worker"),
        ],
        env=environment,
        check=True,
    )
    report = json.loads((args.output / "worker" / "verification.json").read_text(encoding="utf8"))
    speech = [r for r in report["observations"] if r["modality"] == "ASR"]
    normalized = lambda text: " ".join(re.findall(r"\w+", text.lower()))
    target = normalized(args.phrase)
    matches = [r for r in speech if target in normalized(r["text"])]
    offsets = [1_000_000, 59_000_000]
    passed = len(matches) == 2 and all(
        abs(r["range"]["start_us"] - t) <= 2_000_000 for r, t in zip(matches, offsets)
    )
    summary = dict(
        phrase=args.phrase,
        speech_channel="left",
        music_channel="right",
        expected_starts_us=offsets,
        observations=[
            dict(text=r["text"], range=r["range"], support=r["support"], flags=r["quality_flags"])
            for r in speech
        ],
        exact_phrase_occurrences=len(matches),
        passed=passed,
        external_provider_calls=0,
    )
    (args.output / "quality.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf8"
    )
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
