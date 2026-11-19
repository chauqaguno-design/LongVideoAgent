"""Local media evidence for the film index and on-demand clip verification."""

import hashlib
import io
import json
import re
import sqlite3
from contextlib import closing
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory

import av

from .media import embedded_subtitles, speech_batches, srt_ms


def _sample_times(start, end, step):
    if not 0 <= start < end:
        raise ValueError("视频区间需要满足 0 ≤ 起点 < 终点。")
    for left in range(start, end, step):
        yield (left + min(left + step, end)) // 2


def _sample_frames(path, start, end, step):
    """Sample the frame displayed at each instant, including very short final windows."""
    targets = iter(_sample_times(start, end, step))
    target = next(targets)
    with av.open(str(path)) as film:
        stream = film.streams.video[0]
        origin = Fraction(film.start_time or 0, av.time_base)
        if film.duration and end > round(film.duration / 1000):
            raise ValueError("视频区间超出了原片时长。")
        film.seek(int((origin + Fraction(target, 1000)) / stream.time_base), stream=stream)
        previous = None
        for frame in film.decode(stream):
            if frame.pts is None:
                continue
            current = (frame.pts * frame.time_base - origin) * 1000
            while current > target:
                if previous is not None:
                    yield target, previous
                elif current < end:
                    yield round(current), frame
                else:
                    raise ValueError("此区间没有可解码的画面。")
                target = next(targets, None)
                if target is None:
                    return
            previous = frame
        if previous is None:
            raise ValueError("此区间没有可解码的画面。")
        # The final displayed frame extends from its PTS to the end of the film.
        yield target, previous
        for target in targets:
            yield target, previous


def _scaled_frame(frame, pixel_format):
    scale = min(1, 640 / max(frame.width, frame.height))
    width = max(2, int(frame.width * scale) // 2 * 2)
    height = max(2, int(frame.height * scale) // 2 * 2)
    if frame.rotation in (90, -90, 180, -180):
        import numpy as np

        small = frame.reformat(width, height, format="rgb24")
        pixels = np.rot90(small.to_ndarray(), frame.rotation // 90)
        return av.VideoFrame.from_ndarray(pixels, format="rgb24").reformat(format=pixel_format)
    return frame.reformat(width, height, format=pixel_format)


def frames_for_window(path: Path, start_ms: int, end_ms: int) -> list[tuple[int, bytes]]:
    frames = []
    for timestamp, frame in _sample_frames(path, start_ms, end_ms, 5000):
        frame = _scaled_frame(frame, "yuvj420p")
        encoder = av.CodecContext.create("mjpeg", "w")
        encoder.width, encoder.height = frame.width, frame.height
        encoder.pix_fmt, encoder.time_base = "yuvj420p", Fraction(1)
        frames.append((timestamp, bytes(encoder.encode(frame)[0])))
    return frames


def clip_for_window(path: Path, start_ms: int, end_ms: int) -> bytes:
    """Encode a silent 4-fps MP4. Pad short clips with their last sampled frame."""
    output = io.BytesIO()
    with av.open(output, "w", format="mp4") as movie:
        stream = movie.add_stream("libx264", rate=4)
        stream.pix_fmt = "yuv420p"
        stream.options = {"maxrate": "900k", "bufsize": "1200k"}
        previous = None
        count = 0
        for _, source in _sample_frames(path, start_ms, end_ms, 250):
            frame = _scaled_frame(source, "yuv420p")
            if previous is None:
                stream.width, stream.height = frame.width, frame.height
            frame.pts, frame.time_base = count, Fraction(1, 4)
            movie.mux(stream.encode(frame))
            count += 1
            previous = frame
        while count < 8:
            previous.pts = count
            movie.mux(stream.encode(previous))
            count += 1
        movie.mux(stream.encode())
    return output.getvalue()


def read_srt(path: Path) -> list[dict]:
    raw = Path(path).read_bytes()
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        text = raw.decode("gb18030")
    rows = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "").strip()):
        lines = block.splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if len(lines) < 2 or "-->" not in lines[0]:
            continue
        left, right = lines[0].split("-->", 1)
        try:
            start, end = srt_ms(left.strip()), srt_ms(right.strip().split()[0])
        except (ValueError, IndexError):
            continue
        caption = re.sub(r"<[^>]+>|\{\\[^}]*\}", "", " ".join(lines[1:])).strip()
        if end > start >= 0 and caption:
            rows.append({"start_ms": start, "end_ms": end, "text": caption, "source": "外部字幕"})
    if not rows:
        raise ValueError("SRT 中没有可用的带时间戳字幕。")
    return sorted(rows, key=lambda row: row["start_ms"])


def _cached_speech(path, info, root):
    db_file = Path(root) / "cinetrace.sqlite3"
    if not db_file.is_file():
        return None
    with closing(sqlite3.connect(db_file.resolve().as_uri() + "?mode=ro", uri=True)) as db:
        size = path.stat().st_size
        if not db.execute("SELECT 1 FROM videos WHERE size=? LIMIT 1", (size,)).fetchone():
            return None
        digest = info.get("sha256")
        if not digest:
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()
        for (stored,) in db.execute("SELECT context FROM videos WHERE sha256=?", (digest,)):
            context = json.loads(stored)
            state = context.get("tracks", {}).get("speech", {}).get("status")
            if state in {"ready", "empty"}:
                rows = [{key: row[key] for key in ("start_ms", "end_ms", "text", "source")}
                        for row in context.get("evidence", []) if row["track"] == "speech"]
                if rows or state == "empty":
                    return rows
    return None


def dialogue_for_film(path: Path, info: dict, existing_root: Path | None = None) -> list[dict]:
    """Use timed text first, then existing or fresh local ASR; ignore picture text."""
    path = Path(path)
    duration = info["duration_ms"]

    def records(rows):
        return [{"start_ms": max(0, start), "end_ms": min(end, duration), "text": text, "source": source}
                for start, end, text, source in rows if text and max(0, start) < min(end, duration)]

    # Consume a single subtitle track to avoid duplicate translations of each line.
    bitmap = {"dvd_subtitle", "dvb_subtitle", "hdmv_pgs_subtitle", "xsub"}
    for stream in info["subtitles"]:
        if stream["codec"] not in bitmap:
            try:
                subtitles = records(embedded_subtitles(path, stream))
            except (RuntimeError, ValueError):
                continue  # Try another track or local ASR if FFmpeg cannot decode it.
            if subtitles:
                return subtitles
    if not info["has_audio"]:
        return []
    if existing_root is not None:
        speech = _cached_speech(path, info, existing_root)
        if speech is not None:
            return speech
    with TemporaryDirectory() as temp:
        return [row for _, batch in speech_batches(path, Path(temp), info, 0) for row in records(batch)]
