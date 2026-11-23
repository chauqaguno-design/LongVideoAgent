"""Regenerate with independent muxes and compare the decoded source PTS and rasters."""

import os
import shutil

import pytest

from scripts.generate_media_fixtures import generate


@pytest.mark.real_media
def test_regenerated_media_matches_declared_truth(tmp_path):
    ffmpeg, ffprobe = (
        os.getenv("DOVIDEO_" + name.upper()) or shutil.which(name, path=os.getenv("DOVIDEO_FFMPEG_DIR"))
        for name in ("ffmpeg", "ffprobe")
    )
    if not ffmpeg or not ffprobe:
        pytest.skip("Local FFmpeg and ffprobe required")
    a = generate(tmp_path / "first", ffmpeg, ffprobe)
    b = generate(tmp_path / "second", ffmpeg, ffprobe)
    assert a == b
    assert [len(r["rgb_sha256"]) for r in a["fixtures"]] == [12, 6, 12]
