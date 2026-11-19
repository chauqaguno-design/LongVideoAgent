from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from cinetrace.film_index import index_film, read_index


class FilmIndexTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(TemporaryDirectory()))
        self.film = self.root / "source.mp4"
        self.film.write_bytes(b"local source identity")
        self.enterContext(patch("cinetrace.film_index.probe", return_value={"duration_ms": 61_250}))
        self.frames = self.enterContext(patch(
            "cinetrace.film_index.film_media.frames_for_window",
            side_effect=lambda path, start, end: [((start + end) // 2, b"frame")]))
        self.dialogue = self.enterContext(patch(
            "cinetrace.film_index.film_media.dialogue_for_film",
            return_value=[{"start_ms": 29_500, "end_ms": 30_500, "text": "车来了", "source": "字幕"}]))
        self.models = Mock()
        # Deliberately supply made-up model times. Published coordinates must come from the source.
        self.observation = {"scene": "公路", "people": [], "objects": ["车"], "actions": [],
                            "start_ms": 123, "end_ms": 456, "sample_ms": [234]}
        self.models.observe.return_value = self.observation
        self.models.organize.return_value = [
            {"first": 0, "last": 1, "description": "车从远处驶来", "start_ms": 123, "end_ms": 456},
            {"first": 2, "last": 2, "description": "车开过路口", "start_ms": 456, "end_ms": 789}]
        self.models.summarize.return_value = "一辆车驶过路口。"

    def build(self, title="公路"):
        return index_film(self.film, self.root, self.models, title=title, use_web=False)

    def read(self):
        return read_index(self.film, self.root)

    def test_interrupted_build_resumes_from_last_completed_window(self):
        self.models.observe.side_effect = [self.observation, self.observation, RuntimeError("timeout")]
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.build()
        with self.assertRaises(ValueError):
            self.read()

        self.models.observe.side_effect = None
        published = self.build()
        starts = [call.args[1] for call in self.frames.call_args_list]
        self.assertEqual(starts, [0, 30_000, 60_000, 60_000])
        self.dialogue.assert_called_once()
        self.assertEqual(self.read(), published)
        self.assertEqual(self.build(), published)
        self.assertEqual(self.models.observe.call_count, 4)
        self.models.organize.assert_called_once()
        self.models.summarize.assert_called_once()

    def test_rebuild_keeps_published_index_until_replacement_succeeds(self):
        original = self.build()
        self.models.summarize.side_effect = RuntimeError("timeout")
        with self.assertRaisesRegex(RuntimeError, "timeout"):
            self.build(title="同片新资料")
        self.assertEqual(self.read(), original)

        self.models.summarize.side_effect = None
        replacement = self.build(title="同片新资料")
        self.assertEqual(replacement["title"], "同片新资料")
        self.assertEqual(self.read(), replacement)
        self.assertEqual(self.models.observe.call_count, 6)
        self.assertEqual(self.models.organize.call_count, 2)
        self.assertEqual(self.models.summarize.call_count, 3)

    def test_timeline_uses_original_duration_and_source_windows(self):
        with self.assertRaises(ValueError):
            self.read()
        self.build()
        result = self.read()
        self.assertEqual(result["duration_ms"], 61_250)
        self.assertEqual([(e["start_ms"], e["end_ms"]) for e in result["events"]],
                         [(0, 60_000), (60_000, 61_250)])
        self.assertEqual([(w["start_ms"], w["end_ms"], w["sample_ms"], w["transcript"])
                          for w in result["windows"]],
                         [(0, 30_000, [15_000], "车来了"), (30_000, 60_000, [45_000], "车来了"),
                          (60_000, 61_250, [60_625], "")])

    def test_repeated_queries_reuse_file_digest_and_reject_a_replaced_movie(self):
        self.build()
        with patch("cinetrace.film_index.file_hash", side_effect=AssertionError("unnecessary full-file read")):
            self.assertEqual(self.read()["title"], "公路")
        self.film.write_bytes(b"a different local video file")
        with self.assertRaisesRegex(ValueError, "尚未建立"):
            self.read()
