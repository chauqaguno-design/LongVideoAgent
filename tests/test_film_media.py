import hashlib
import io
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import av
import numpy as np

from cinetrace.film_media import clip_for_window, dialogue_for_film, frames_for_window, read_srt


class FilmMediaTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        temp = TemporaryDirectory()
        cls.addClassCleanup(temp.cleanup)
        cls.root = Path(temp.name)
        cls.video = cls.root / "silent.mp4"
        with av.open(str(cls.video), "w") as movie:
            stream = movie.add_stream("libx264", rate=4)
            stream.width, stream.height = 64, 48
            for index in range(24):
                pixels = np.full((48, 64, 3), 32 if index < 4 else 215, dtype=np.uint8)
                movie.mux(stream.encode(av.VideoFrame.from_ndarray(pixels, format="rgb24")))
            movie.mux(stream.encode())

    def test_sparse_frames_include_short_tail_and_use_absolute_timestamps(self):
        frames = frames_for_window(self.video, 500, 6000)
        self.assertEqual([timestamp for timestamp, _ in frames], [3000, 5750])
        for _, jpeg in frames:
            self.assertTrue(jpeg.startswith(b"\xff\xd8"))
        self.assertEqual(frames_for_window(self.video, 5999, 6000)[0][0], 5999)

    def test_short_clip_padding_never_includes_the_next_shot(self):
        for start, end, level in [(0, 1000, 32), (1000, 1120, 215), (5999, 6000, 215)]:
            with self.subTest(start=start), av.open(io.BytesIO(clip_for_window(self.video, start, end))) as movie:
                self.assertEqual(len(movie.streams.audio), 0)
                self.assertEqual(movie.duration, 2_000_000)
                frames = list(movie.decode(video=0))
                self.assertEqual(len(frames), 8)
                for frame in frames:
                    self.assertAlmostEqual(frame.to_ndarray(format="rgb24").mean(), level, delta=3)

    def test_external_srt_orders_lines_and_skips_invalid_cues(self):
        srt = self.root / "subtitles.srt"
        srt.write_text("2\n00:00:01,400 --> 00:00:02,100\n<i>再见</i>\n\n"
                       "坏时间 --> 00:00:01,000\n损坏项\n\n"
                       "1\n00:00:00,100 --> 00:00:01,000\n你好\n", encoding="utf-8-sig")
        rows = read_srt(srt)
        self.assertEqual([(r["start_ms"], r["text"]) for r in rows], [(100, "你好"), (1400, "再见")])

    def test_no_audio_or_subtitles_yields_empty_dialogue(self):
        info = {"duration_ms": 6000, "subtitles": [], "has_audio": False}
        with patch("cinetrace.film_media.speech_batches", side_effect=AssertionError):
            self.assertEqual(dialogue_for_film(self.video, info), [])

    def test_legacy_cache_requires_matching_content_and_speech_track(self):
        info = {"duration_ms": 6000, "subtitles": [], "has_audio": True}
        speech = {"start_ms": 10, "end_ms": 100, "text": "一句话", "source": "Whisper"}
        evidence = [dict(speech, track="speech"), dict(speech, track="subtitle", text="招牌")]
        cached = {"tracks": {"speech": {"status": "ready"}}, "evidence": evidence}
        size = self.video.stat().st_size
        digest = hashlib.sha256(self.video.read_bytes()).hexdigest()
        with closing(sqlite3.connect(self.root / "cinetrace.sqlite3")) as db:
            db.execute("CREATE TABLE videos(size INTEGER, sha256 TEXT, context TEXT)")
            db.execute("INSERT INTO videos VALUES (?,?,?)", (size, "wrong-source", "invalid-json"))
            db.execute("INSERT INTO videos VALUES (?,?,?)", (size, digest, json.dumps(cached)))
            db.commit()
        with patch("cinetrace.film_media.speech_batches", side_effect=AssertionError):
            self.assertEqual(dialogue_for_film(self.video, info, self.root), [speech])

    def test_unreadable_subtitle_track_falls_back_to_asr(self):
        info = {"duration_ms": 6000, "subtitles": [{"codec": "mov_text"}], "has_audio": True}
        with patch("cinetrace.film_media.embedded_subtitles", side_effect=RuntimeError), \
             patch("cinetrace.film_media.speech_batches", return_value=[(6000, [(300, 7000, "话", "ASR")])]):
            self.assertEqual(dialogue_for_film(self.video, info),
                             [{"start_ms": 300, "end_ms": 6000, "text": "话", "source": "ASR"}])
