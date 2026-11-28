"""Compile source PTS into a fixed CFR edit and render a validated video."""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from tempfile import TemporaryDirectory

from .edit_captions import Cue, map_cues, parse_srt, soft_caption_windows, write_srt
from .media.process import command

US = 1_000_000
RENDERER_VERSION = "cutsieve-ffmpeg-v3"
# FFmpeg encoder names, ffprobe codec names and the soft subtitle encoder.
CODECS = {"mp4": ("libx264", "aac", "h264", "aac", "mov_text"),
          "mkv": ("libx264", "flac", "h264", "flac", "srt"),
          "webm": ("libvpx-vp9", "libopus", "vp9", "opus", "webvtt")}


def renderer_version(container):
    return "cutsieve-webm-v4" if container == "webm" else RENDERER_VERSION


def media_binaries(environ=None):
    values = os.environ if environ is None else environ
    directory = values.get("CUTSIEVE_FFMPEG_DIR") or values.get("FFMPEG_DIR")
    binaries = {}
    for tool in ("ffmpeg", "ffprobe"):
        prefix = f"CUTSIEVE_{tool.upper()}"
        configured = values.get(prefix) or values.get(f"{prefix}_PATH")
        if not configured and directory:
            configured = shutil.which(tool, path=directory)
        binaries[tool] = configured or tool
    return binaries


def output_frame(duration_us, fps):
    """Nearest cumulative CFR boundary, with ties rounded to even."""
    return round(Fraction(duration_us, US) * fps)


# Accept self-contained video containers. In particular, do not open playlists.
SOURCE_FLAGS = ["-protocol_whitelist", "file", "-format_whitelist",
                "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,mpegts,mpeg,flv,ogg,asf,nut"]


def _run(args, *, cwd=None, timeout=900, stopped=None):
    output, _ = command(args, stopped, cwd=cwd, timeout=timeout)
    return output.decode("utf8", "replace")


def _seconds(seconds):
    """FFmpeg duration arguments accept decimal seconds, not rational strings."""
    return f"{float(seconds):.9f}"


def _origin(meta):
    return Fraction(meta["source_origin"]["num"], meta["source_origin"]["den"])


def check_source_frame(path, stream, ffmpeg):
    output = _run([ffmpeg,"-v","error","-xerror","-nostdin","-threads","1",*SOURCE_FLAGS,
                   "-i",str(path),"-map",f"0:{stream}","-frames:v","1","-an","-vf","scale=16:16",
                   "-threads","1","-f","framehash","-"],timeout=60)
    if not any(line.startswith("0,") for line in output.splitlines()):
        raise ValueError("No decodable presentation frame")


def probe_media(source: Path, *, ffprobe="ffprobe", stopped=None) -> dict:
    raw = json.loads(_run([ffprobe, "-v", "error", *SOURCE_FLAGS, "-show_format",
                           "-show_streams", "-of", "json", str(Path(source).resolve())], stopped=stopped))
    tracks = [s for s in raw["streams"] if s["codec_type"] in ("video", "audio")
              and not s.get("disposition", {}).get("attached_pic")]
    video = next((s for s in tracks if s["codec_type"] == "video"), None)
    if not video:
        raise ValueError("Source contains no video track")
    streams, starts, ends = [], [], []
    for stream in tracks:
        tb = Fraction(stream["time_base"])
        if "start_pts" in stream:
            pts = int(stream["start_pts"])
        else:
            sample = _run([ffprobe, "-v", "error", *SOURCE_FLAGS, "-select_streams",
                           str(stream["index"]), "-read_intervals", "%+#1", "-show_packets",
                           "-show_entries", "packet=pts", "-of", "csv=p=0", str(source)], stopped=stopped)
            if not sample.strip() or sample.startswith("N/A"):
                raise ValueError("Media stream has no presentation timestamp")
            pts = int(sample.split(",")[0].strip())
        starts.append(pts * tb)
        if "duration_ts" in stream:
            ends.append((pts + int(stream["duration_ts"])) * tb)
        streams.append(dict(kind=stream["codec_type"], codec=stream["codec_name"],
                            time_base_num=tb.numerator, time_base_den=tb.denominator,
                            start_pts=pts))
    origin = min(starts)
    fmt = raw["format"]
    if len(ends) == len(tracks):
        duration = max(ends) - origin
    else:
        duration = Fraction(fmt["duration"])
        # Matroska's segment duration includes a nonzero initial timestamp.
        if "matroska" in fmt["format_name"]:
            duration -= origin
    rotation = next((int(d["rotation"]) % 360 for d in video.get("side_data_list", [])
                     if "rotation" in d), 0)
    width, height = int(video["width"]), int(video["height"])
    if rotation in (90, 270):
        width, height = height, width
    audio = next((s for s in tracks if s["codec_type"] == "audio"), None)
    return dict(duration_us=round(duration * US), streams=streams,
                source_origin=dict(num=origin.numerator, den=origin.denominator),
                width=width, height=height, rotation=rotation,
                sample_aspect_ratio=video.get("sample_aspect_ratio", "1:1"),
                average_frame_rate=video.get("avg_frame_rate"),
                video_time_base=video["time_base"], video_index=video["index"],
                audio_index=audio["index"] if audio else None,
                has_subtitles=any(s["codec_type"] == "subtitle" for s in raw["streams"]),
                browser_playable=("mp4" in fmt["format_name"] and video["codec_name"] == "h264"
                                  and video.get("pix_fmt") in ("yuv420p", "yuvj420p")
                                  and (not audio or audio["codec_name"] in ("aac", "mp3"))))


@dataclass(frozen=True)
class Cut:
    source: Path
    meta: dict
    clip: dict
    first_pts: int
    end_pts: int
    output_start: int
    output_end: int


def _boundary(source, meta, time_us, *, end, ffprobe, stopped=None):
    """Pick displayed frame at start / exclusive frame at end, using decoded PTS."""
    tb = Fraction(meta["video_time_base"])
    target = _origin(meta) + Fraction(time_us, US)
    # Seek to the previous keyframe and normally decode just the nearby GOP.
    # If a sparse VFR stream has a larger gap, scan on to its true next frame.
    left = max(_origin(meta), target - 1)
    rate = meta.get("average_frame_rate")
    cadence = Fraction(rate) if rate and rate != "0/0" else 0
    lookahead = max(2, 4 / cadence) if cadence > 0 else 2
    media_end = _origin(meta) + Fraction(meta["duration_us"], US)
    retried_seek = False
    while True:
        # Always give ffprobe an explicit end. In FFmpeg 5.1, an open-ended
        # read interval can silently return only a prefix of an intact GOP.
        through_end = target + lookahead >= media_end
        stop = _seconds(target + lookahead)
        # At the stream origin read normally. An explicit seek to that point is unnecessary
        # and some FFmpeg/demuxer combinations occasionally return an empty decode.
        begin = _seconds(left) if left > _origin(meta) else ""
        frames = json.loads(_run([
            ffprobe, "-v", "error", "-threads", "1", *SOURCE_FLAGS, "-select_streams", str(meta["video_index"]),
            "-read_intervals", f"{begin}%{stop}", "-show_frames", "-show_entries",
            "frame=pts,best_effort_timestamp,pkt_duration,duration", "-of", "json", str(source),
        ], stopped=stopped))["frames"]
        pts = sorted({int(f["pts"] if "pts" in f else f["best_effort_timestamp"]) for f in frames})
        if pts and (pts[-1] >= target / tb or through_end):
            if not end:
                return pts[max(0, bisect_right(pts, target / tb) - 1)]
            pos = bisect_left(pts, target / tb)
            if pos < len(pts):
                return pts[pos]
            last = frames[-1]
            return pts[-1] + int(last.get("duration", last.get("pkt_duration", 0)))
        if through_end:
            if not pts and not retried_seek:
                left = max(_origin(meta), left - lookahead)
                retried_seek = True
                continue
            raise ValueError("No decodable frames at the selected source time")
        # A packet-time cutoff can exclude displayed B frames immediately before the target.
        lookahead *= 4


def compile_timeline(revision, sources, *, job_id, profile, ffprobe="ffprobe", stopped=None):
    """Round cumulative duration to the nearest CFR frame (ties to even); retain source PTS."""
    if profile not in ("PREVIEW", "EXPORT") or not revision["clips"]:
        raise ValueError("Rendering needs a nonempty timeline and a known profile")
    output = revision["output_profile"]
    fps = Fraction(output["fps_num"], output["fps_den"])
    if min(output["width"], output["height"]) < 2 or any(output[k] % 2 for k in ("width", "height")):
        raise ValueError("H.264 dimensions must be positive and even")
    if output["audio_sample_rate"] != 48000 or not 0 < fps <= 120:
        raise ValueError("Unsupported output frame rate or audio sample rate")
    cuts, mappings, metadata = [], [], {}
    elapsed = frame = 0
    for clip in revision["clips"]:
        media_id = clip["media_revision_id"]
        src = Path(sources[media_id]).resolve()
        if src not in metadata:
            metadata[src] = probe_media(src, ffprobe=ffprobe, stopped=stopped)
        meta = metadata[src]
        start, end = clip["source_start_us"], clip["source_end_us"]
        if not 0 <= start < end <= meta["duration_us"]:
            raise ValueError("Clip extends beyond the immutable source")
        crop = clip.get("crop")
        if crop and (min(crop["x"], crop["y"]) < 0 or min(crop["width"], crop["height"]) <= 0
                     or crop["x"] + crop["width"] > meta["width"]
                     or crop["y"] + crop["height"] > meta["height"]):
            raise ValueError("Crop extends beyond the displayed source")
        first = _boundary(src, meta, start, end=False, ffprobe=ffprobe, stopped=stopped)
        after = _boundary(src, meta, end, end=True, ffprobe=ffprobe, stopped=stopped)
        elapsed += end - start
        next_frame = output_frame(elapsed, fps)
        if next_frame <= frame or after <= first:
            raise ValueError("The clip is shorter than one usable output frame")
        cuts.append(Cut(src, meta, clip, first, after, frame, next_frame))
        tb, origin = Fraction(meta["video_time_base"]), _origin(meta)
        mappings.append(dict(clip_id=clip["clip_id"], media_revision_id=media_id,
                             requested_source_range=dict(start_us=start, end_us=end),
                             realized_source_range=dict(start_us=round((first * tb - origin) * US),
                                                        end_us=round((after * tb - origin) * US)),
                             output_start_us=round(frame * US / fps),
                             output_end_us=round(next_frame * US / fps),
                             output_start_frame=frame, output_end_frame=next_frame))
        frame = next_frame
    manifest = dict(revision_id=revision["revision_id"], render_job_id=job_id, profile=profile,
                    renderer_version=RENDERER_VERSION, output_profile=output, clips=mappings,
                    total_duration_us=mappings[-1]["output_end_us"])
    return cuts, manifest

def _video_filters(cut, output, fps):
    clip = cut.clip
    absolute = _origin(cut.meta) + Fraction(clip["source_start_us"], US)
    filters = [f"trim=start_pts={cut.first_pts}:end_pts={cut.end_pts}",
               f"setpts=PTS-({_seconds(absolute)})/TB"]
    if crop := clip.get("crop"):
        filters.append("crop={width}:{height}:{x}:{y}:exact=1".format(**crop))
    # Normalise non-square pixels *before* fitting the result to the output canvas.
    w, h = output["width"], output["height"]
    filters.extend(["scale=w='max(2,trunc(iw*sar/2)*2)':h=ih", "setsar=1",
                    f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2",
                    f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2", "setsar=1", "format=yuv420p",
                    f"fps={fps}:start_time=0:round=up", "tpad=stop=-1:stop_mode=clone",
                    f"trim=end_frame={cut.output_end-cut.output_start}", "setpts=PTS-STARTPTS"])
    return ",".join(filters)


def _audio_filters(cut, index, fps):
    """Both streams use the same clip origin; preserve leading silence after a delayed audio start."""
    clip = cut.clip
    samples = round(cut.output_end * 48000 / fps) - round(cut.output_start * 48000 / fps)
    if clip["audio_policy"] == "mute" or cut.meta["audio_index"] is None:
        return f"anullsrc=r=48000:cl=stereo,atrim=end_sample={samples}"
    origin = _origin(cut.meta)
    start = _seconds(origin + Fraction(clip["source_start_us"], US))
    end = _seconds(origin + Fraction(clip["source_end_us"], US))
    return (f"[{index}:{cut.meta['audio_index']}]atrim=start={start}:end={end},"
            f"asetpts=PTS-({start})/TB,aresample=48000:async=1:first_pts=0,"
            f"aformat=channel_layouts=stereo,apad,atrim=end_sample={samples}")


def _subtitles(cuts, manifest, revision, tracks, ffmpeg, stopped):
    if revision["caption_policy"] == "none":
        return []
    embedded, mapped = {}, []
    for cut, mapping in zip(cuts, manifest["clips"]):
        clip = cut.clip
        if track := clip.get("caption_track_id"):
            if track not in tracks:
                raise ValueError("Selected caption track is unavailable")
            cues = tracks[track]  # resolver supplies cues in canonical source microseconds
        elif cut.meta["has_subtitles"]:
            if cut.source not in embedded:
                srt = _run([ffmpeg, "-nostdin", "-v", "error", "-copyts", *SOURCE_FLAGS,
                            "-i", str(cut.source), "-map", "0:s:0", "-f", "srt", "pipe:1"], stopped=stopped)
                origin_us = round(_origin(cut.meta) * US)
                embedded[cut.source] = [Cue(c.start_us-origin_us, c.end_us-origin_us, c.text)
                                        for c in parse_srt(srt)]
            cues = embedded[cut.source]
        else:
            cues = []
        mapped.extend(map_cues(cues, clip["source_start_us"], clip["source_end_us"],
                               mapping["output_start_us"], mapping["output_end_us"]))
    return mapped


def _validate_output(target, manifest, ffmpeg, ffprobe, stopped, soft_cues):
    container = Path(target).suffix[1:].lower()
    count_packets = [] if container == "mp4" else ["-count_packets"]
    raw = json.loads(_run([ffprobe, "-v", "error", *count_packets, "-show_format", "-show_streams",
                           "-of", "json", str(target)], stopped=stopped))
    video = next((s for s in raw["streams"] if s["codec_type"] == "video"), None)
    audio = next((s for s in raw["streams"] if s["codec_type"] == "audio"), None)
    if not video or bool(audio) != manifest.get("has_audio", True):
        raise ValueError("Output streams differ from the compiled video and audio layout")
    _, _, video_codec, audio_codec, _ = CODECS[container]
    video_codecs = {video_codec, "av1"} if container == "webm" else {video_codec}
    muxer = "mp4" if container == "mp4" else "matroska"
    if (muxer not in raw["format"]["format_name"].split(",") or
            video["codec_name"] not in video_codecs or (audio and audio["codec_name"] != audio_codec)):
        raise ValueError("Output container or codecs differ from the export format")
    if container == "webm":
        expected = [("video", video["codec_name"])]
        if audio:
            expected.append(("audio", audio_codec))
        if soft_cues:
            expected.append(("subtitle", "webvtt"))
        if [(s["codec_type"], s["codec_name"]) for s in raw["streams"]] != expected:
            raise ValueError("Output contains unexpected WebM streams")
    p = manifest["output_profile"]
    fps = Fraction(p["fps_num"], p["fps_den"])
    frames = manifest["clips"][-1]["output_end_frame"]
    if ((video["width"], video["height"]) != (p["width"], p["height"]) or
            Fraction(video["avg_frame_rate"]) != fps or int(video.get("nb_frames") or video["nb_read_packets"]) != frames):
        raise ValueError("Encoded video disagrees with the compiled output grid")
    if audio:
        if "duration_ts" in audio:
            audio_seconds = int(audio["duration_ts"]) * Fraction(audio["time_base"])
        else:
            # Matroska stores stream end times as HH:MM:SS.nnnnnnnnn tags.
            hours, minutes, seconds = audio["tags"]["DURATION"].split(":")
            audio_seconds = int(hours) * 3600 + int(minutes) * 60 + Fraction(seconds)
        if int(audio["sample_rate"]) != 48000 or abs(audio_seconds - frames / fps) > Fraction(1024, 48000):
            raise ValueError("Output audio duration differs from the compiled timeline")
    audio_map = ["-map", "0:a:0"] if audio else []
    _run([ffmpeg, "-nostdin", "-v", "error", "-xerror", "-threads","2", "-i", str(target),
          "-map", "0:v:0", *audio_map, "-f", "null", "-"], stopped=stopped)
    if soft_cues:
        if not any(s["codec_type"] == "subtitle" for s in raw["streams"]):
            raise ValueError("Output is missing its compiled subtitle track")
        actual = parse_srt(_run([ffmpeg, "-nostdin", "-v", "error", "-copyts", "-i", str(target),
                                 "-map", "0:s:0", "-f", "srt", "-"], stopped=stopped))
        if [(c.start_us, c.end_us) for c in actual] != [(c.start_us, c.end_us) for c in soft_cues]:
            raise ValueError("Output subtitle times differ from the compiled cues")
    return round(Fraction(raw["format"]["duration"]) * US)


def inspect_output(target, manifest, *, ffmpeg, ffprobe, soft_cues=(), stopped=None):
    """Inspect decoded media independently of any semantic judgment about the requested edit."""
    report = dict(timeline_revision_id=manifest["revision_id"], soft_cue_count=len(soft_cues))
    with Path(target).open("rb") as handle:
        report["output_sha256"] = hashlib.file_digest(handle, "sha256").hexdigest()
    try:
        report["duration_us"] = _validate_output(target, manifest, ffmpeg, ffprobe, stopped, soft_cues)
    except ValueError as exc:
        return dict(report, status="FAILED", issue=str(exc))
    return dict(report, status="PASSED")


def render_timeline(revision, sources, output, *, job_id, profile="PREVIEW",
                    ffmpeg="ffmpeg", ffprobe="ffprobe", caption_tracks=None, font_path=None,
                    audit=None, validating=None, stopped=None):
    """Render into a private staging directory and publish after full decoding."""
    cuts, manifest = compile_timeline(revision, sources, job_id=job_id, profile=profile, ffprobe=ffprobe, stopped=stopped)
    p = manifest["output_profile"]
    fps = Fraction(p["fps_num"], p["fps_den"])
    policy = revision["caption_policy"]
    if policy not in ("none", "preserve_source", "burn_aligned"):
        raise ValueError("Unknown caption policy")
    cues = _subtitles(cuts, manifest, revision, caption_tracks or {}, ffmpeg, stopped)
    if policy == "preserve_source":
        cues = soft_caption_windows(cues)
    version = _run([ffmpeg, "-version"], stopped=stopped).splitlines()[0]
    major = re.match(r"ffmpeg version (\d+)", version)
    graph_flag = "-/filter_complex" if major and int(major[1]) >= 7 else "-filter_complex_script"
    output = Path(output).resolve()
    container = output.suffix[1:].lower()
    video_encoder, audio_encoder, _, _, subtitle_encoder = CODECS[container]
    has_audio = True
    if container == "webm":
        # A silent Opus track still counts as audio for browser autoplay.
        has_audio = any(cut.clip["audio_policy"] == "keep" for cut in cuts)
        manifest.update(renderer_version=renderer_version(container), has_audio=has_audio)
        encoders = _run([ffmpeg, "-hide_banner", "-encoders"], stopped=stopped)
        if not re.search(r"\blibvpx-vp9\b", encoders):
            video_encoder = "libaom-av1"
    output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=output.parent) as tmp:
        stage = Path(tmp)
        args = [ffmpeg, "-nostdin", "-hide_banner", "-v", "error", "-xerror",
                "-filter_complex_threads", "2", "-copyts"]
        graph = []
        for i, cut in enumerate(cuts):
            # Input seek keeps a two-second preroll and the preceding keyframe.
            seek = max(0, cut.clip["source_start_us"] - 2 * US)
            if seek:
                args.extend(["-ss", _seconds(Fraction(seek, US)), "-noaccurate_seek"])
            args.extend([*SOURCE_FLAGS, "-threads", "2", "-i", str(cut.source)])
            graph.append(f"[{i}:{cut.meta['video_index']}]" + _video_filters(cut, p, fps) + f"[v{i}]")
            if has_audio:
                graph.append(_audio_filters(cut, i, fps) + f"[a{i}]")
        n = len(cuts)
        videos = "".join(f"[v{i}]" for i in range(n))
        video = f"{videos}concat=n={n}:v=1:a=0,settb={fps.denominator}/{fps.numerator},setpts=N"
        if has_audio:
            audios = "".join(f"[a{i}]" for i in range(n))
            graph.append(f"{audios}concat=n={n}:v=0:a=1,asetpts=N/SR/TB[aout]")
        if cues:
            write_srt(stage / "cues.srt", cues)
        if policy == "burn_aligned":
            if not cues or not font_path or not Path(font_path).is_file():
                raise ValueError("Burn-in requires aligned caption cues and a configured font")
            # drawtext uses this exact font file; cue text is data, never a filter expression.
            shutil.copyfile(font_path, stage / "font.ttf")
            for i, cue in enumerate(cues):
                (stage / f"cue{i}.txt").write_text(cue.text, encoding="utf8")
                video += (f",drawtext=fontfile=font.ttf:textfile=cue{i}.txt:expansion=none:"
                          f"fontsize={max(14, p['height']//20)}:fontcolor=white:borderw=2:"
                          f"x=(w-tw)/2:y=h-th-h/15:enable='gte(t,{_seconds(cue.start_us/US)})"
                          f"*lt(t,{_seconds(cue.end_us/US)})'")
        graph.append(video + "[vout]")
        (stage / "graph.txt").write_text(";\n".join(graph), encoding="utf8")
        if cues and policy == "preserve_source":
            args.extend(["-i", "cues.srt"])
        args.extend([graph_flag, "graph.txt", "-map", "[vout]"])
        if has_audio:
            args.extend(["-map", "[aout]", "-c:a", audio_encoder, "-ar", "48000", "-ac", "2"])
        if cues and policy == "preserve_source":
            args.extend(["-map", f"{n}:s:0", "-c:s", subtitle_encoder])
        filename = "output." + container
        args.extend(["-map_metadata", "-1", "-metadata:s:v:0", "rotate=0", "-c:v", video_encoder,
                     "-pix_fmt", "yuv420p", "-r", str(fps)])
        if container == "webm":
            args.extend(["-f", "webm", "-cpu-used", "4" if video_encoder == "libvpx-vp9" else "8",
                         "-crf", "32", "-b:v", "0"])
        else:
            args.extend(["-preset", "veryfast" if profile == "PREVIEW" else "medium",
                         "-crf", "26" if profile == "PREVIEW" else "18"])
        if container == "mp4":
            args.extend(["-movflags", "+faststart"])
        else:
            args.extend(["-avoid_negative_ts", "disabled"])
        args.extend(["-threads", "2", filename])
        _run(args, cwd=stage, stopped=stopped)
        if validating:
            validating()
        target = stage / filename
        check = inspect_output(target, manifest, ffmpeg=ffmpeg, ffprobe=ffprobe, stopped=stopped,
                               soft_cues=cues if policy == "preserve_source" else ())
        if check["status"] != "PASSED":
            raise ValueError(check["issue"])
        if audit is not None:
            audit.update(full_decode=True, encoder=version, structure=check,
                         source_frames=[dict(clip_id=c.clip["clip_id"], start_pts=c.first_pts,
                                             end_pts=c.end_pts, time_base=c.meta["video_time_base"],
                                             source_origin=c.meta["source_origin"]) for c in cuts])
        target.replace(output)
    return manifest
