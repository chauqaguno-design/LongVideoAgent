import unittest
from unittest.mock import Mock, patch

from cinetrace.film_search import find_scene


def scene(start, text, transcript=""):
    return {"start_ms": start, "end_ms": start + 30_000, "scene": text, "people": [],
            "objects": [], "actions": [], "transcript": transcript}


class FilmSearchTests(unittest.TestCase):
    def setUp(self):
        self.models = Mock()
        self.clip = self.enterContext(patch("cinetrace.film_search.film_media.clip_for_window", return_value=b"film"))

    def find(self, query, rows, budget=3):
        return find_scene("original.mp4", query, rows, {}, self.models, budget=budget)

    def test_rejection_changes_search_and_uses_source_times(self):
        rows = [scene(0, "车旁有人说话"), scene(30_000, "两辆车在公路上并排行驶")]
        self.models.expand.side_effect = [["车旁"], ["并排行驶"]]
        self.models.verify.side_effect = [
            {"status": "reject", "reason": "车停在路边。", "missing": ["正在竞速的车辆"]},
            {"status": "match", "reason": "两辆车在公路上交替超车。", "missing": []},
        ]
        result = self.find("飙车", rows)
        self.assertEqual(result["status"], "verified")
        self.assertEqual(result["match"]["start_ms"], 30_000)
        self.assertIn("车停在路边", self.models.expand.call_args.args[2][-1])
        self.assertEqual([r["status"] for r in result["trace"] if r["step"] == "verify"], ["reject", "match"])

    def test_partial_rechecks_neighbor_and_respects_budget(self):
        rows = [scene(0, "车库", "与复核区间不完全重合的台词"), scene(30_000, "追车", "上车。")]
        self.models.expand.return_value = ["追车"]
        self.models.verify.return_value = {"status": "partial", "reason": "拍到追车，无法确认人物。", "missing": ["人物身份"]}
        result = self.find("谁在追车", rows, budget=2)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["verification_calls"], 2)
        self.assertEqual(result["stop_reason"], "budget_exhausted")
        self.assertTrue(result["trace"][-1]["with_neighbors"])
        self.clip.assert_called_with("original.mp4", 22_000, 60_000)
        self.assertEqual(self.models.verify.call_args.args[2], "上车。")

    def test_no_index_hit_and_zero_budget_do_not_call_vision(self):
        self.models.expand.return_value = ["赛车"]
        unseen = self.find("飙车", [scene(0, "房间里有书桌")])
        self.assertEqual((unseen["status"], unseen["match"], unseen["stop_reason"]), ("no_match", None, "no_index_hit"))
        self.assertEqual(self.find("赛车", [scene(0, "赛车")], budget=0)["verification_calls"], 0)
        self.models.expand.assert_called_once()
        self.models.verify.assert_not_called()
        self.clip.assert_not_called()

    def test_english_words_do_not_match_substrings(self):
        self.models.expand.return_value = ["car"]
        self.assertEqual(self.find("car", [scene(0, "scarlet")])["stop_reason"], "no_index_hit")

    def test_neighbor_verification_uses_time_aligned_dialogue(self):
        rows = [scene(0, "车库"), scene(30_000, "大家开始追车")]
        dialogue = [{"start_ms": 500, "end_ms": 700, "text": "区间以外"},
                    {"start_ms": 23_000, "end_ms": 25_000, "text": "我是小安"}]
        self.models.expand.return_value = ["追车"]
        self.models.verify.side_effect = [
            {"status": "partial", "reason": "人物身份不明", "missing": ["身份"]},
            {"status": "match", "reason": "看见自我介绍和追车", "missing": []}]
        result = find_scene("original.mp4", "小安在哪里追车", rows, {}, self.models, budget=2, dialogue=dialogue)
        self.assertEqual(result["status"], "verified")
        self.assertEqual([call.args[2] for call in self.models.verify.call_args_list], ["", "我是小安"])
