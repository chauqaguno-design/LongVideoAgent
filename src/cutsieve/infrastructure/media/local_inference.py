"""Run explicitly provisioned offline speech/OCR models in their own Python env.

Called with a worker-created JSON manifest. Each result is atomic so a restart
can reuse completed chunks without reloading the model for each frame.
"""

import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys
import unicodedata
import wave


def asr_model_config(env):
    candidates = (env.get("CUTSIEVE_WHISPER_DIR"), env.get("CUTSIEVE_WHISPER_MODEL_ROOT"))
    root = next((str(Path(path).resolve()) for path in candidates if path and Path(path).is_dir()), "")
    interpreter = env.get("CUTSIEVE_ASR_PYTHON") or env.get("CUTSIEVE_MEDIA_PYTHON") or sys.executable
    return root, interpreter


def aligned_words(model, audio, text, language, offset):
    """Align an existing transcript to its own audio span with Whisper's acoustic model."""
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer

    if len(audio) < model.feature_extractor.n_fft or not text.strip():
        return []
    tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, "transcribe", language)
    features = model.feature_extractor(audio)
    tokens = tokenizer.encode(" " + text.strip())
    # A span which cannot be aligned as a whole retains its original segment times.
    if not tokens or len(tokens) + len(tokenizer.sot_sequence) >= model.max_length:
        return []
    frames = features.shape[-1]
    if frames > model.feature_extractor.nb_max_frames:
        return []
    try:
        alignment = model.find_alignment(tokenizer, [tokens], model.encode(pad_or_trim(features)), frames)[
            0
        ]
    except (RuntimeError, ValueError, IndexError):
        return []
    words = []
    previous = 0.0
    for item in alignment:
        start, end = float(item["start"]), float(item["end"])
        if not previous <= start <= end <= len(audio) / model.feature_extractor.sampling_rate:
            return []
        word = item["word"]
        if words and all(unicodedata.category(c).startswith("P") for c in word.strip()):
            words[-1]["text"] += word
        else:
            words.append(dict(start=start + offset, end=end + offset, text=word))
        previous = end
    return words


def read_pcm16(path, rate):
    # The worker has already decoded the source to mono PCM. Reading that WAV
    # directly also avoids relying on the optional PyAV input API.
    with wave.open(str(path), "rb") as audio:
        if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) != (1, 2, rate):
            raise ValueError(f"Whisper input must be mono PCM16 at {rate} Hz")
        return audio.readframes(audio.getnframes())


def build_asr(root):
    import numpy as np

    from faster_whisper import WhisperModel
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    model = WhisperModel(
        str(root),
        device="cpu",
        compute_type="int8",
        cpu_threads=4,
        local_files_only=True,
    )

    def infer(path):
        rate = 16_000
        audio = np.frombuffer(read_pcm16(path, rate), dtype="<i2").astype(np.float32) / 32768
        vad = VadOptions(min_silence_duration_ms=800, max_speech_duration_s=28)
        regions = get_speech_timestamps(audio, vad, sampling_rate=rate)
        result = {
            "language": None,
            "vad_regions": regions,
            "sample_rate": rate,
            "spans": [],
        }
        # Keep distant speech islands separate. Joining them before decoding can move a word
        # by a minute or smear two identical lines into one continuous transcript.
        for region in regions:
            left, right = region["start"], region["end"]
            offset = left / rate
            spans, info = model.transcribe(
                audio[left:right],
                vad_filter=False,
                word_timestamps=True,
                condition_on_previous_text=False,
                beam_size=3,
            )
            result["language"] = info.language
            for s in spans:
                if not s.text.strip():
                    continue
                words = [
                    dict(start=w.start + offset, end=w.end + offset, text=w.word) for w in (s.words or ())
                ]
                timing = "recognizer"
                if not words:
                    start = left + round(s.start * rate)
                    end = min(right, left + round(s.end * rate))
                    words = aligned_words(model, audio[start:end], s.text, info.language, start / rate)
                    timing = "whisper-acoustic-alignment" if words else "segment"
                result["spans"].append(
                    dict(
                        start=max(offset, s.start + offset),
                        end=min(right / rate, s.end + offset),
                        text=s.text.strip(),
                        language=info.language,
                        no_speech_prob=s.no_speech_prob,
                        avg_logprob=s.avg_logprob,
                        compression_ratio=s.compression_ratio,
                        word_timing=timing,
                        words=words,
                    )
                )
        return result

    return "faster-whisper", version("faster-whisper"), infer


def build_ocr(root):
    # The model paths are local. Disable Paddle's connectivity probe as well.
    os.environ["PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK"] = "True"
    os.environ["PADDLE_PDX_CACHE_HOME"] = str(root.parent)
    from paddleocr import PaddleOCR

    model = PaddleOCR(
        device="cpu",
        text_detection_model_dir=str(root / "PP-OCRv5_mobile_det"),
        text_recognition_model_dir=str(root / "PP-OCRv5_mobile_rec"),
        text_detection_model_name="PP-OCRv5_mobile_det",
        text_recognition_model_name="PP-OCRv5_mobile_rec",
        use_doc_orientation_classify=False,
        use_doc_unwarping=False,
        use_textline_orientation=False,
        enable_mkldnn=False,
        cpu_threads=4,
    )

    def infer(path):
        result = next(iter(model.predict(str(path))))
        lines = [
            dict(text=text, confidence=float(score), polygon=polygon.tolist())
            for text, score, polygon in zip(result["rec_texts"], result["rec_scores"], result["rec_polys"])
            if text.strip()
        ]
        return {"lines": lines}

    return "paddleocr", version("paddleocr"), infer


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    cfg = json.loads(parser.parse_args().manifest.read_text(encoding="utf-8"))
    root = Path(cfg["model"])
    pending = [entry for entry in cfg["inputs"] if not Path(entry["output"]).is_file()]
    if not pending:
        return
    name, ver, infer = (build_asr if cfg["mode"] == "asr" else build_ocr)(root)
    for item in pending:
        output = Path(item["output"])
        record = infer(Path(item["path"]))
        record.update(model=name, version=ver, checkpoint=cfg["checkpoint"])
        stage = output.with_suffix(f".{os.getpid()}.tmp")
        stage.write_text(json.dumps(record, ensure_ascii=False), encoding="utf8")
        stage.replace(output)


if __name__ == "__main__":
    main()
