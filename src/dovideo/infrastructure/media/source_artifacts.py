"""Bounded local decoding with a common presentation origin for all outputs."""

from fractions import Fraction
import hashlib
from pathlib import Path
import re

from PIL import Image

from dovideo.infrastructure.edit_captions import Cue, parse_srt
from dovideo.infrastructure.edit_media import SOURCE_FLAGS, US, _boundary, _origin, probe_media
from .process import command


def digest_file(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


class SourceReader:
    def __init__(self, path, probe, tools, stopped):
        self.path, self.probe, self.stopped = path, probe, stopped
        self.ffmpeg, self.ffprobe = tools["ffmpeg"], tools["ffprobe"]
        self.raw_timebase = Fraction(probe["video_time_base"])
        self.origin = _origin(probe)
        self.version = command([self.ffmpeg, "-version"], stopped)[0].decode().splitlines()[0].split(" ")[2]
        if probe["width"] * probe["height"] > 40_000_000:
            raise ValueError("源画面过大")

    def ff(self, pre=(), post=(), *, log="error", timeout=900):
        return command(
            [
                self.ffmpeg,
                "-nostdin",
                "-hide_banner",
                "-loglevel",
                log,
                "-y",
                "-threads",
                "2",
                *pre,
                *SOURCE_FLAGS,
                "-i",
                self.path,
                *post,
            ],
            self.stopped,
            timeout=timeout,
        )

    def frame_pts(self, requested):
        return _boundary(self.path, self.probe, requested, end=False, ffprobe=self.ffprobe, stopped=self.stopped)

    def save_frame(self, pts, output):
        actual = round((pts * self.raw_timebase - self.origin) * US)
        seek = max(0, (actual - 2 * US) / US)
        self.ff(
            ["-copyts", "-ss", str(seek), "-noaccurate_seek"],
            [
                "-map",
                f"0:{self.probe['video_index']}",
                "-vf",
                f"select='eq(pts,{pts})'",
                "-vsync",
                "0",
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-threads",
                "1",
                output,
            ],
        )
        with Image.open(output) as image:
            image.verify()
        return actual

    def scene_points(self, start, end):
        _, err = self.ff(
            ["-ss", str(start / US), "-t", str((end - start) / US)],
            [
                "-map",
                f"0:{self.probe['video_index']}",
                "-an",
                "-vf",
                "scale=160:-2,select='gt(scene,0.32)',showinfo",
                "-vsync",
                "0",
                "-frames:v",
                "4",
                "-f",
                "null",
                "-",
            ],
            log="info",
        )
        return [
            start + round(float(t) * US)
            for t in re.findall(rb"\bpts_time:([-+\d.eE]+)", err)
            if 0 <= float(t) < (end - start) / US
        ][:4]

    def subtitle_text(self):
        if not self.probe["has_subtitles"]:
            return None, []
        data, _ = self.ff(["-copyts"], ["-map", "0:s:0", "-c:s", "srt", "-f", "srt", "-"])
        text = data.decode("utf-8-sig")
        origin = round(self.origin * US)
        cues = [
            Cue(max(0, c.start_us - origin), min(self.probe["duration_us"], c.end_us - origin), c.text)
            for c in parse_srt(text)
        ]
        return data, [c for c in cues if c.end_us > c.start_us and c.text.strip()]

    def audio(self, start, end, output):
        # Decode only the relevant GOP, preserving blank time before a delayed audio stream.
        origin = float(self.origin) + start / US
        samples = round((end - start) * 16_000 / US)
        graph = (
            f"atrim=start={origin:.9f}:end={float(self.origin)+end/US:.9f},"
            f"asetpts=PTS-({origin:.9f})/TB,aresample=16000:async=1:first_pts=0,"
            f"apad=whole_len={samples},atrim=end_sample={samples}"
        )
        idx = self.probe["audio_index"]
        self.ff(
            ["-copyts", "-ss", str(max(0, start / US - 2)), "-noaccurate_seek"],
            ["-map", f"0:{idx}", "-af", graph, "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", output],
        )

    def proxy(self, output):
        offset = f"({float(self.origin):.9f})/TB"
        post = [
            "-map",
            f"0:{self.probe['video_index']}",
            "-vf",
            f"setpts=PTS-{offset},scale='min(1280,iw)':-2,scale=trunc(iw/2)*2:trunc(ih/2)*2,format=yuv420p",
            "-vsync",
            "0",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-threads",
            "2",
        ]
        if self.probe["audio_index"] is not None:
            post.extend(
                ["-map", f"0:{self.probe['audio_index']}", "-af", f"asetpts=PTS-{offset}", "-c:a", "aac"]
            )
        self.ff(["-copyts"], [*post, "-movflags", "+faststart", output], timeout=3600)
        checked = probe_media(output, ffprobe=self.ffprobe, stopped=self.stopped)
        if (
            not checked["browser_playable"]
            or abs(checked["duration_us"] - self.probe["duration_us"]) > 100_000
        ):
            raise ValueError("代理视频时长与原片不符")
        return {
            "version": 1,
            "source_origin": self.probe["source_origin"],
            "proxy_origin": checked["source_origin"],
            "source_duration_us": self.probe["duration_us"],
            "proxy_duration_us": checked["duration_us"],
            "mapping": "normalized presentation microseconds",
            "streams": checked["streams"],
        }
