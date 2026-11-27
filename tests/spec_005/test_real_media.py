"""M1 media acceptance on fixed synthetic signals; never calls an external model."""

import os
from fractions import Fraction
import shutil
import subprocess
from uuid import uuid4

import pytest

from cutsieve.infrastructure.edit_captions import Cue, map_cues
from cutsieve.infrastructure.edit_media import compile_timeline, probe_media, render_timeline

pytestmark = pytest.mark.real_media


def uid():
    return str(uuid4())


@pytest.fixture(scope="module")
def tools():
    directory = os.getenv("CUTSIEVE_FFMPEG_DIR")
    result = {}
    for name in ("ffmpeg", "ffprobe"):
        exe = os.getenv(f"CUTSIEVE_{name.upper()}") or shutil.which(name, path=directory)
        if not exe:
            pytest.skip(f"Set CUTSIEVE_FFMPEG_DIR or add {name} to PATH")
        result[name] = exe
    return result


def ff(exe, *args):
    return subprocess.run([exe, "-v", "error", *map(str, args)], check=True, capture_output=True).stdout


@pytest.fixture
def movies(tmp_path, tools):
    ffmpeg = tools["ffmpeg"]
    red, blue = tmp_path / "red tone.mp4", tmp_path / "蓝色.mp4"
    ff(ffmpeg, "-f", "lavfi", "-i", "color=red:s=64x48:r=25:d=2",
       "-itsoffset", "0.3", "-f", "lavfi", "-i", "sine=frequency=800:sample_rate=48000:d=1.4",
       "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-output_ts_offset", "5", red)
    ff(ffmpeg, "-f", "lavfi", "-i", "color=blue:s=48x64:r=12:d=2", "-c:v", "libx264", blue)
    return red, blue


def clip(media_id, start, end, audio="keep"):
    return dict(clip_id=uid(), media_revision_id=media_id, source_start_us=start,
                source_end_us=end, audio_policy=audio, evidence_ids=[])


def revision(clips, fps=25):
    return dict(revision_id=uid(), clips=clips, caption_policy="none", output_profile=dict(
        width=64, height=48, fps_num=fps, fps_den=1, audio_sample_rate=48000))


def pixel(ffmpeg, path, seconds):
    color = ff(ffmpeg, "-ss", str(seconds), "-i", path, "-map", "0:v:0", "-vf", "crop=2:2",
               "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "pipe:1")
    return max(range(3), key=color.__getitem__)


def loud(ffmpeg, path, seconds):
    samples = ff(ffmpeg, "-ss", str(seconds), "-i", path, "-t", "0.05", "-map", "0:a:0",
                 "-ac", "1", "-f", "s16le", "pipe:1")
    peak = max(abs(int.from_bytes(samples[i:i+2], "little", signed=True))
               for i in range(0, len(samples), 2))
    return peak > 300


def test_preview_export_order_pts_delayed_audio_and_silence(tmp_path, movies, tools):
    a, b = uid(), uid()
    sources = dict(zip((a,b), movies))
    probe = probe_media(movies[0], ffprobe=tools["ffprobe"])
    assert probe["source_origin"] == {"num": 5, "den": 1}
    assert probe["duration_us"] == 2_000_000
    assert [s["kind"] for s in probe["streams"]] == ["video", "audio"]
    shot = clip(a, 0, 800_000)
    blue = clip(b, 70_000, 470_000)
    muted = clip(a, 600_000, 1_000_000, "mute")
    for profile, order, colors, tones in [
        ("PREVIEW", [blue, shot, muted], [(0.1,2),(0.5,0)], [(0.1,False),(0.5,False),(0.95,True),(1.4,False)]),
        ("EXPORT", [muted, shot, blue], [(0.1,0),(1.4,2)], [(0.1,False),(0.5,False),(0.95,True),(1.4,False)]),
    ]:
        target = tmp_path / (profile + ".mp4")
        audit = {}
        manifest = render_timeline(revision(order), sources, target, job_id=uid(), profile=profile,
                                   audit=audit, **tools)
        assert audit["full_decode"] and manifest["total_duration_us"] == 1_600_000
        blue_mapping = next(row for row in manifest["clips"] if row["clip_id"] == blue["clip_id"])
        assert blue_mapping["realized_source_range"] == {"start_us":0, "end_us":500_000}
        assert [pixel(tools["ffmpeg"], target, t) for t, _ in colors] == [c for _,c in colors]
        assert [loud(tools["ffmpeg"], target, t) for t, _ in tones] == [v for _,v in tones]


def test_vfr_and_rotation_crop(tmp_path, tools):
    raw, rotated = tmp_path / "vfr.mp4", tmp_path / "rotated.mp4"
    ff(tools["ffmpeg"], "-f", "lavfi", "-i", "color=red:s=64x48:r=10:d=2",
       "-vf", "drawbox=x=32:y=0:w=32:h=48:color=blue:t=fill,select='not(between(n,4,7))'",
       "-fps_mode", "vfr", "-c:v", "libx264", raw)
    if b"-display_rotation" in ff(tools["ffmpeg"], "-h", "full"):
        ff(tools["ffmpeg"], "-display_rotation", "90", "-i", raw, "-c", "copy", rotated)
    else:
        ff(tools["ffmpeg"], "-i", raw, "-c", "copy", "-metadata:s:v:0", "rotate=90", rotated)
    mid = uid()
    piece = clip(mid, 511_000, 905_000)
    piece["crop"] = dict(x=0,y=0,width=48,height=32)
    rev = revision([piece])
    assert probe_media(rotated, ffprobe=tools["ffprobe"])["width"] == 48
    out = tmp_path / "result.mp4"
    report = render_timeline(rev, {mid:rotated}, out, job_id=uid(), **tools)
    assert report["clips"][0]["realized_source_range"] == {"start_us":300_000,"end_us":1_000_000}
    assert pixel(tools["ffmpeg"], out, 0.1) == 2
    assert probe_media(out, ffprobe=tools["ffprobe"])["rotation"] == 0
    piece["crop"]["x"] = 10
    with pytest.raises(ValueError, match="Crop"):
        compile_timeline(rev,{mid:rotated},job_id=uid(),profile="EXPORT",ffprobe=tools["ffprobe"])


@pytest.mark.parametrize("container", ["mp4", "webm", "mkv"])
def test_overlapping_soft_captions_survive_mux_and_validation(tmp_path, movies, tools, container):
    from cutsieve.infrastructure.edit_captions import parse_srt

    mid, track = uid(), uid()
    cut = dict(clip(mid, 0, 1_000_000), caption_track_id=track)
    rev = dict(revision([cut]), caption_policy="preserve_source")
    target = tmp_path / ("overlap." + container)
    cues = [Cue(100000, 600000, "ALPHA"), Cue(400000, 700000, "BETA")]
    render_timeline(rev, {mid: movies[0]}, target, job_id=uid(), profile="EXPORT", caption_tracks={track: cues}, **tools)
    decoded = ff(tools["ffmpeg"], "-copyts", "-i", target, "-map", "0:s:0", "-f", "srt", "-").decode()
    assert parse_srt(decoded) == [
        Cue(100000, 400000, "ALPHA"), Cue(400000, 600000, "ALPHA\nBETA"), Cue(600000, 700000, "BETA")
    ]


def test_subtitle_track_reorder_and_intersection(tmp_path, movies, tools):
    mid, track = uid(), uid()
    later, earlier = clip(mid, 500_000, 900_000), clip(mid, 100_000, 500_000)
    for p in (later, earlier):
        p["caption_track_id"] = track
    rev = revision([later, earlier])
    rev["caption_policy"] = "preserve_source"
    target = tmp_path/"subtitled.mp4"
    render_timeline(rev, {mid:movies[0]}, target, job_id=uid(),
                    caption_tracks={track:[Cue(200_000,700_000,"Hello")]}, **tools)
    text = ff(tools["ffmpeg"], "-i", target, "-map", "0:s:0", "-f", "srt", "pipe:1").decode()
    assert "00:00:00,000 --> 00:00:00,200" in text
    assert "00:00:00,500 --> 00:00:00,800" in text
    assert map_cues([Cue(0, 1, "absent")],1,2,0,1) == []

def test_late_seek_rational_fps_and_frozen_vfr(tmp_path, tools):
    source = tmp_path/"long offset.mp4"
    ff(tools["ffmpeg"], "-f", "lavfi", "-i", "color=red:s=64x48:r=25:d=8",
       "-vf", "drawbox=color=blue:t=fill:enable='gte(t,4)',select='not(between(n,130,145))'",
       "-fps_mode", "vfr", "-output_ts_offset", "5", "-c:v", "libx264", source)
    mid = uid()
    rev = revision([clip(mid, 5_300_000, 5_450_000), clip(mid, 1_100_000, 1_700_000)])
    rev["output_profile"].update(fps_num=30000, fps_den=1001)
    target = tmp_path/"late.mp4"
    result = render_timeline(rev,{mid:source},target,job_id=uid(),**tools)
    assert pixel(tools["ffmpeg"],target,0.03) == 2
    assert pixel(tools["ffmpeg"],target,0.30) == 0
    assert result["clips"][0]["realized_source_range"] == {"start_us":5_160_000,"end_us":5_840_000}
    assert result["clips"][-1]["output_end_frame"] == round(Fraction(750_000*30000,1_000_000*1001))


def test_preserve_embedded_subs_and_burn_font_failure(tmp_path, movies, tools):
    source = tmp_path/"CC.mp4"
    (tmp_path/"src.srt").write_text("1\n00:00:00,100 --> 00:00:00,900\nWords\n",encoding="utf8")
    ff(tools["ffmpeg"], "-i", movies[1], "-i", tmp_path/"src.srt", "-map", "0:v", "-map","1:s",
       "-c:v","copy","-c:s","mov_text",source)
    mid = uid()
    rev = revision([clip(mid,200_000,600_000)])
    rev["caption_policy"]="preserve_source"
    target=tmp_path/"kept.mp4"
    render_timeline(rev,{mid:source},target,job_id=uid(),**tools)
    assert "00:00:00,000 --> 00:00:00,400" in ff(tools["ffmpeg"],"-i",target,"-map","0:s:0",
                                                  "-f","srt","pipe:1").decode()
    rev["caption_policy"]="burn_aligned"
    with pytest.raises(ValueError,match="configured font"):
        render_timeline(rev,{mid:source},tmp_path/"invalid.mp4",job_id=uid(),**tools)
    assert not (tmp_path/"invalid.mp4").exists()

def test_keep_on_either_side_of_audio_stream_is_silent(tmp_path, tools):
    source = tmp_path/"partial-audio.mp4"
    ff(tools["ffmpeg"], "-f", "lavfi", "-i", "color=red:s=64x48:r=25:d=3",
       "-itsoffset", "1", "-f", "lavfi", "-i", "sine=duration=0.5",
       "-c:v", "libx264", "-c:a", "aac", source)
    mid = uid()
    cut = revision([clip(mid,0,500_000), clip(mid,2_000_000,2_800_000),
                    clip(mid,1_100_000,1_300_000)])
    target = tmp_path/"partial.mp4"
    report = render_timeline(cut,{mid:source},target,job_id=uid(),**tools)
    assert report["total_duration_us"] == 1_520_000
    assert [loud(tools["ffmpeg"],target,t) for t in (0.15,0.85,1.42)] == [False,False,True]
