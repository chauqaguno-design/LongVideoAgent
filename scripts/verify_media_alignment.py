"""Run the offline missing-word-time fallback against a known spoken recording."""

import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
from unittest.mock import patch
import wave

import numpy as np

from faster_whisper import WhisperModel
from faster_whisper.audio import decode_audio


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("model", type=Path)
    parser.add_argument("wav", type=Path)
    parser.add_argument("phrase")
    parser.add_argument("report", type=Path)
    parser.add_argument("--language", default="en")
    args = parser.parse_args()
    path = Path(__file__).resolve().parents[1] / "src/dovideo/infrastructure/media/local_inference.py"
    spec = importlib.util.spec_from_file_location("local_inference", path)
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    model = WhisperModel(
        str(args.model),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        local_files_only=True,
    )
    audio = decode_audio(str(args.wav), sampling_rate=16_000)
    offset = 10.0
    words = adapter.aligned_words(model, audio, args.phrase, args.language, offset)
    duration = len(audio) / 16_000
    assert "".join(w["text"] for w in words).strip() == args.phrase
    assert all(offset <= w["start"] <= w["end"] <= offset + duration for w in words)
    assert len({round(w["end"] - w["start"], 3) for w in words}) > 1
    assert not adapter.aligned_words(model, audio[:1], args.phrase, args.language, offset)
    # Exercise the production branch with a real transcription restricted to segment times.
    transcribe = WhisperModel.transcribe

    def segment_times(self, *a, **kwargs):
        return transcribe(self, *a, **dict(kwargs, word_timestamps=False))

    with patch.object(WhisperModel, "transcribe", segment_times):
        _, _, infer = adapter.build_asr(args.model)
        response = infer(args.wav)
    assert response["spans"] and any(
        s["word_timing"] == "whisper-acoustic-alignment" for s in response["spans"]
    )
    t = np.arange(48_000) / 16_000
    negatives = {}
    with tempfile.TemporaryDirectory() as folder:
        for name, samples in {
            "silence": t * 0,
            "music": 0.21 * np.sin(2 * np.pi * 220 * t),
        }.items():
            wav = Path(folder) / f"{name}.wav"
            with wave.open(str(wav), "wb") as f:
                f.setparams((1, 2, 16_000, 0, "NONE", ""))
                f.writeframes((samples * 32767).astype("<i2").tobytes())
            negatives[name] = infer(wav)
            assert not negatives[name]["spans"]
    result = dict(
        phrase=args.phrase,
        offset=offset,
        duration=duration,
        words=words,
        model=args.model.name,
        actual_segment_only_response=response,
        no_voice_controls=negatives,
    )
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf8")
    print(f"Aligned {len(words)} words within {duration:.2f}s; unusable audio kept segment timing.")


if __name__ == "__main__":
    main()
