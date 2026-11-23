from fractions import Fraction
import os
import shutil
import subprocess
from threading import Event

from PIL import Image
import pytest
from dovideo.application.media_sampling import frame_quotas
from dovideo.infrastructure.edit_media import probe_media
from dovideo.infrastructure.media.source_artifacts import SourceReader


def test_fair_budget_keeps_tail_when_media_is_longer_than_budget():
    quotas = frame_quotas(120, 10)
    assert sum(quotas) == 10 and quotas[0] == quotas[-1] == 1
    assert min(frame_quotas(120, 500)) >= 4
    assert max(frame_quotas(120, 500)) <= 5


@pytest.mark.real_media
def test_source_frame_matches_pts_for_vfr_nonzero_origin(tmp_path):
    tools = {
        name: os.getenv("DOVIDEO_" + name.upper())
        or shutil.which(name, path=os.getenv("DOVIDEO_FFMPEG_DIR"))
        for name in ("ffmpeg", "ffprobe")
    }
    if not all(tools.values()):
        pytest.skip("FFmpeg required")
    src = tmp_path / "中文 [片段].mkv"
    subprocess.run(
        [
            tools["ffmpeg"],
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x48:r=10:d=1",
            "-vf",
            "select='eq(n,0)+eq(n,2)+eq(n,7)+eq(n,9)'",
            "-vsync",
            "vfr",
            "-c:v",
            "ffv1",
            "-output_ts_offset",
            "5",
            str(src),
        ],
        check=True,
    )
    meta = probe_media(src, ffprobe=tools["ffprobe"])
    reader = SourceReader(src, meta, tools, Event())
    assert meta["source_origin"] == dict(num=5, den=1)
    times = [reader.frame_pts(t) for t in [201_000, 680_000, 715_000, 999_999]]
    for n, (pts, expected) in enumerate(zip(times, [200000, 200000, 700000, 900000])):
        out = tmp_path / f"{n}.jpg"
        assert reader.save_frame(pts, out) == expected
        assert Image.open(out).size == (64, 48)
    assert times[0] == times[1]


@pytest.mark.real_media
def test_reordered_sparse_h264_brackets_each_requested_frame(tmp_path):
    tools = {
        n: os.getenv("DOVIDEO_" + n.upper()) or shutil.which(n, path=os.getenv("DOVIDEO_FFMPEG_DIR"))
        for n in ("ffmpeg", "ffprobe")
    }
    if not all(tools.values()):
        pytest.skip("FFmpeg required")
    video = tmp_path / "sparse.mp4"
    subprocess.run(
        [
            tools["ffmpeg"],
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=s=64x48:r=1/15:d=120",
            "-c:v",
            "libx264",
            "-g",
            "4",
            "-bf",
            "3",
            "-x264-params",
            "b-adapt=0:scenecut=0",
            str(video),
        ],
        check=True,
    )
    meta = probe_media(video, ffprobe=tools["ffprobe"])
    # Existing revisions lack the frame-rate hint, so both probe paths must agree.
    for rate in (meta["average_frame_rate"], None):
        reader = SourceReader(video, dict(meta, average_frame_rate=rate), tools, Event())
        for second in range(0, 120, 15):
            raw_pts = reader.frame_pts((second + 7) * 1_000_000)
            assert raw_pts * reader.raw_timebase == second


@pytest.mark.real_media
def test_frame_uses_the_original_display_rotation(tmp_path):
    tools = {n: shutil.which(n, path=os.getenv("DOVIDEO_FFMPEG_DIR")) for n in ("ffmpeg", "ffprobe")}
    if not all(tools.values()):
        pytest.skip("FFmpeg required")
    original = tmp_path / "unrotated.mp4"
    rotated = tmp_path / "rotated.mp4"
    subprocess.run(
        [
            tools["ffmpeg"],
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=blue:s=96x64:d=1,drawbox=x=0:y=0:w=48:h=64:color=red:t=fill",
            "-c:v",
            "libx264",
            str(original),
        ],
        check=True,
    )
    subprocess.run(
        [
            tools["ffmpeg"],
            "-v",
            "error",
            "-i",
            str(original),
            "-c",
            "copy",
            "-metadata:s:v:0",
            "rotate=90",
            str(rotated),
        ],
        check=True,
    )
    meta = probe_media(rotated, ffprobe=tools["ffprobe"])
    assert meta["rotation"] == 90 and (meta["width"], meta["height"]) == (64, 96)
    reader = SourceReader(rotated, meta, tools, Event())
    path = tmp_path / "frame.jpg"
    assert reader.save_frame(reader.frame_pts(0), path) == 0
    with Image.open(path) as image:
        assert image.size == (64, 96)
        assert image.getpixel((32, 16))[2] > 230 and image.getpixel((32, 80))[0] > 230
