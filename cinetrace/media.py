"""Read one film as bounded batches of time-stamped observations."""

import json
import os
import re
import shutil
import subprocess
from functools import lru_cache
from pathlib import Path
from tempfile import TemporaryDirectory

import av

from examples.describe_clip import post_clip, source_events


class Unavailable(RuntimeError):
    """A track needs an optional local model or a configured cloud account."""


def probe(path):
    with av.open(str(path), format="mov") as film:
        if not film.streams.video:
            raise ValueError("文件没有视频轨。")
        video = film.streams.video[0]
        if next(film.decode(video), None) is None:
            raise ValueError("视频轨没有可解码的画面。")
        duration_ms = round((film.duration or 0) / 1000)
        if duration_ms <= 0 and video.duration is not None:
            duration_ms = round(video.duration * video.time_base * 1000)
        if duration_ms <= 0:
            raise ValueError("无法读取原片时长。")
        return {
            "duration_ms": duration_ms,
            "has_audio": bool(film.streams.audio),
            "subtitles": [
                {"index": s.index, "codec": s.codec_context.name, "language": s.metadata.get("language", "und")}
                for s in film.streams.subtitles
            ],
        }


def ffmpeg(*args):
    executable = os.getenv("CINETRACE_FFMPEG") or shutil.which("ffmpeg")
    if not executable:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", *map(str, args)],
        capture_output=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.decode("utf-8", "replace").strip()[-500:] or "FFmpeg 读取失败")
    return result.stdout


def windows(start, duration, step):
    for begin in range(start, duration, step):
        yield begin, min(begin + step, duration)


def shot_windows(path, start, duration):
    """Find hard cuts in bounded batches, then divide long shots into short observations."""
    filters = "scale=160:-2,select='gt(scene,0.27)',metadata=print:key=lavfi.scene_score:file=-"
    for begin, end in windows(start, duration, 30_000):
        metadata = ffmpeg("-ss", begin / 1000, "-t", (end - begin) / 1000, "-i", path,
                          "-map", "0:v:0", "-an", "-vf", filters, "-f", "null", "-")
        cuts = [begin + round(float(s) * 1000) for s in re.findall(rb"pts_time:([\d.]+)", metadata)]
        boundaries = [begin, *(t for t in cuts if begin < t < end), end]
        for left, right in zip(boundaries, boundaries[1:]):
            count = (right - left + 3999) // 4000
            for n in range(count):
                yield left + n * (right - left) // count, left + (n + 1) * (right - left) // count


@lru_cache(maxsize=1)
def whisper(name):
    try:
        from faster_whisper import WhisperModel
    except ImportError as exc:
        raise Unavailable("安装 faster-whisper 后可以重试语音转写。") from exc
    return WhisperModel(name, device="cpu", compute_type="int8")


def speech_batches(path, temp, info, start):
    if not info["has_audio"]:
        return
    model_name = os.getenv("CINETRACE_WHISPER_MODEL", "turbo")
    model = whisper(model_name)
    for begin, end in windows(start, info["duration_ms"], 120_000):
        wav = temp / "audio.wav"
        ffmpeg("-ss", begin / 1000, "-i", path, "-t", (end - begin) / 1000, "-map", "0:a:0",
               "-af", "aresample=16000:async=1:first_pts=0", "-ac", 1, "-ar", 16000,
               "-c:a", "pcm_s16le", wav)
        segments, _ = model.transcribe(str(wav), vad_filter=True, language=os.getenv("CINETRACE_LANGUAGE"))
        rows = [(begin + round(s.start * 1000), min(end, begin + round(s.end * 1000)),
                 s.text.strip(), f"Whisper · {Path(model_name).name}") for s in segments]
        yield end, rows


def local_describer(directory):
    with (Path(directory) / "config.json").open(encoding="utf-8") as config:
        model_type = json.load(config)["model_type"]
    if model_type == "qwen3_vl":
        from .qwen_vision import describe
        return describe, "Qwen3-VL · 镜头内多帧（估计区间）"
    if model_type == "smolvlm":
        from .local_vision import describe
        return describe, "SmolVLM2 · 代表帧观察（片段范围）"
    raise Unavailable(f"暂不支持此本地视觉模型类型：{model_type}")


def visual_batches(path, temp, info, start):
    key, url = os.getenv("DASHSCOPE_API_KEY"), os.getenv("DASHSCOPE_BASE_URL")
    local_model = os.getenv("CINETRACE_LOCAL_VISION")
    cloud = bool(key and url)
    if not cloud and not local_model:
        raise Unavailable("配置本地视觉模型 CINETRACE_LOCAL_VISION 或百炼 API 后可分析画面。")
    if not cloud:
        describe, model_label = local_describer(local_model)
    duration_ms = info["duration_ms"]
    periods = windows(start, duration_ms, 30_000) if cloud else shot_windows(path, start, duration_ms)
    for begin, end in periods:
        duration = (end - begin) / 1000
        clip = temp / "visual.mp4"
        # Trim the input before padding so a short shot never borrows frames from the next one.
        length = max(2 if cloud else 1, duration)
        fps = 4 if cloud else 6
        ffmpeg("-ss", begin / 1000, "-t", duration, "-i", path, "-map", "0:v:0", "-an",
               "-vf", f"fps={fps}:round=up,scale=640:640:force_original_aspect_ratio=decrease:force_divisible_by=2,tpad=stop_mode=clone:stop_duration=2",
               "-t", length, "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", 26, clip)
        if cloud:
            response = post_clip(clip, length, url, key)
            observations = source_events(response, length, begin)
            rows = [(o["start_ms"], min(end, o["end_ms"]), o["action"], "百炼 · qwen3-vl-plus")
                    for o in observations if o["start_ms"] < end]
        else:
            rows = [(begin, end, describe(clip, local_model), model_label)]
        yield end, rows


def srt_ms(value):
    hours, minutes, seconds, millis = map(int, re.split("[:,.]", value))
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + millis


def embedded_subtitles(path, stream):
    srt = ffmpeg("-i", path, "-map", f"0:{stream['index']}", "-f", "srt", "pipe:1")
    for block in re.split(r"\n\s*\n", srt.decode("utf-8-sig").replace("\r", "").strip()):
        lines = block.splitlines()
        if len(lines) >= 3:
            begin, end = lines[1].split(" --> ")
            text = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", "\n".join(lines[2:])).strip()
            yield srt_ms(begin), srt_ms(end.split()[0]), text, f"内封字幕 · {stream['language']} · 轨 {stream['index']}"


def subtitle_batches(path, temp, info, start):
    if start >= info["duration_ms"]:
        return
    bitmap_codecs = {"dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "xsub"}
    text_streams = [s for s in info["subtitles"] if s["codec"] not in bitmap_codecs]
    if text_streams:
        yield info["duration_ms"], [row for s in text_streams for row in embedded_subtitles(path, s)]
        return
    if os.getenv("CINETRACE_OCR") != "1":
        raise Unavailable("没有内封文本字幕。设置 CINETRACE_OCR=1 可识别画面字幕。")
    try:
        from paddleocr import PaddleOCR
    except ImportError as exc:
        raise Unavailable("安装 PaddlePaddle 和 PaddleOCR 后可重试字幕识别。") from exc
    ocr = PaddleOCR(device="cpu", text_detection_model_name="PP-OCRv5_mobile_det",
                    text_recognition_model_name="PP-OCRv5_mobile_rec", use_doc_orientation_classify=False,
                    use_doc_unwarping=False, use_textline_orientation=False)
    for begin, end in windows(start, info["duration_ms"], 60_000):
        with TemporaryDirectory(dir=temp) as frames:
            filters = "fps=1:start_time=0,crop=iw:ih*0.4:0:ih-oh"
            source = "烧录字幕 OCR（采样估计）"
            if info["subtitles"]:
                # Extract pixels from the bitmap subtitle plane rather than reading the background.
                index = info["subtitles"][0]["index"]
                canvas = f"[0:v]drawbox=color=black:t=fill[bg];[bg][0:{index}]overlay,{filters.split(',')[0]}[out]"
                output = ["-filter_complex", canvas, "-map", "[out]"]
                source = "图像字幕 OCR（采样估计）"
            else:
                output = ["-map", "0:v:0", "-vf", filters]
            ffmpeg("-ss", begin / 1000, "-i", path, "-t", (end - begin) / 1000,
                   *output, "-an", Path(frames) / "%05d.jpg")
            rows = []
            for i, frame in enumerate(sorted(Path(frames).glob("*.jpg"))):
                text = " ".join(t for result in ocr.predict(str(frame)) for t in result["rec_texts"]).strip()
                timestamp = begin + i * 1000
                if text:
                    rows.append((timestamp, min(timestamp + 1000, end), text, source))
            yield end, rows
