import unittest
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from cinetrace.pipeline import timeline
from cinetrace.presentation import chinese_actions
from cinetrace.storage import Store
from cinetrace.translation import needs_translation


class ActionLanguageTests(unittest.TestCase):
    def test_existing_action_and_scene_are_displayed_in_chinese(self):
        english = "A woman walks between two men."
        chinese = "一名女子走到两名男子之间。"
        rows = [
            {"id": "speech-0", "track": "speech", "text": "Wait a minute.", "source": "Whisper",
             "start_ms": 8850, "end_ms": 10200},
            {"id": "subtitles-1", "track": "subtitles", "text": "One moment.", "source": "内封字幕",
             "start_ms": 8900, "end_ms": 10100},
            {"id": "visual-2", "track": "visual", "text": english, "source": "SmolVLM2 · 英文画面观察（窗口范围）",
             "start_ms": 9000, "end_ms": 12000},
        ]
        chunks, scenes = timeline(15000, rows)
        saved = {"evidence": rows, "chunks": chunks, "scenes": scenes, "index": {"version": "existing"}}
        before = deepcopy(saved)

        with patch("cinetrace.presentation.translate_action", side_effect={english: chinese}.__getitem__):
            display = chinese_actions(saved)

        action = display["evidence"][2]
        self.assertEqual(action["text"], chinese)
        self.assertEqual(action["original_text"], english)
        self.assertNotIn("英文画面观察", action["source"])
        for field in ("id", "track", "start_ms", "end_ms"):
            self.assertEqual(action[field], rows[2][field])
        self.assertEqual(display["evidence"][:2], rows[:2])
        self.assertEqual(display["chunks"], chunks)
        self.assertEqual(display["index"], saved["index"])
        self.assertEqual(display["scenes"][0]["evidence_ids"], scenes[0]["evidence_ids"])
        self.assertEqual(display["scenes"][0]["summary"], scenes[0]["summary"].replace(english, chinese))
        self.assertEqual(saved, before)

    def test_translation_survives_restart(self):
        with TemporaryDirectory() as data:
            with patch("cinetrace.translation.translate_action", return_value="她关上门。") as model:
                self.assertEqual(Store(Path(data)).translate_action("She closes the door."), "她关上门。")
                self.assertEqual(Store(Path(data)).translate_action("She closes the door."), "她关上门。")
                model.assert_called_once_with("She closes the door.")

    def test_mixed_language_labels(self):
        self.assertTrue(needs_translation('A man points to a sign marked “出口”.'))
        self.assertFalse(needs_translation("他把 CPU 放在桌上。"))
