import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from cinetrace import pipeline
from cinetrace.media import shot_windows


class VisualRefreshTests(unittest.TestCase):
    @patch("cinetrace.media.ffmpeg", return_value=b"frame:0 pts:123 pts_time:3.433333\n")
    def test_windows_cover_source_and_stay_within_each_shot(self, _):
        spans = list(shot_windows("movie.mp4", 30_000, 39_000))
        self.assertEqual(spans, [(30000, 33433), (33433, 36216), (36216, 39000)])

    def setUp(self):
        speech = {"id": "speech-1", "track": "speech", "start_ms": 0,
                  "end_ms": 9000, "text": "等等。", "source": "Whisper"}
        visual = {**speech, "id": "visual-0", "track": "visual", "text": "屋里有人。"}
        self.context = {"tracks": {"visual": {"status": "ready", "refresh": {"rows": []}},
                                   "speech": {"status": "ready"}, "subtitles": {"status": "empty"}},
                        "evidence": [speech, visual], "index": {"status": "ready", "version": "old"}}
        self.context["chunks"], self.context["scenes"] = pipeline.timeline(9000, [speech, visual])

    def refresh(self, extractor):
        saved = []
        with TemporaryDirectory() as tmp, patch("cinetrace.pipeline.probe", return_value={"duration_ms": 9000}), \
                patch.dict(pipeline.TRACKS, visual=("画面动作", extractor)), \
                patch("cinetrace.indexing.build_index", return_value={"status": "ready", "version": "new"}):
            result = pipeline.build_context(Path("original.mp4"), tmp, deepcopy(self.context),
                                            lambda item: saved.append(deepcopy(item)))
        return saved, result

    def test_replace_only_visual_and_relink_index(self):
        rows = [(0, 3500, "男子朝桌边伸手。", "测试"), (3500, 9000, "女子转头。", "测试")]
        saved, result = self.refresh(Mock(return_value=[(9000, rows)]))
        self.assertEqual(saved[1]["evidence"], self.context["evidence"])
        self.assertEqual(result["status"], "ready")
        self.assertIn(self.context["evidence"][0], result["evidence"])
        self.assertNotIn("visual-0", result["chunks"][0]["evidence_ids"])
        self.assertIn("女子转头", result["scenes"][0]["summary"])
        self.assertEqual(result["index"]["version"], "new")

    def test_failure_preserves_playable_evidence_and_index(self):
        _, result = self.refresh(Mock(side_effect=RuntimeError("视觉模型不可用")))
        self.assertIn("视觉模型不可用", result["error"])
        self.assertNotIn("refresh", result["tracks"]["visual"])
        for key in ("evidence", "chunks", "scenes", "index"):
            self.assertEqual(result[key], self.context[key])
