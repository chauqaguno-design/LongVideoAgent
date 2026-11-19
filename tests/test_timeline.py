import unittest

from cinetrace.pipeline import timeline


class TimelineTests(unittest.TestCase):
    def test_crossing_action_remains_one_piece_of_evidence(self):
        evidence = [
            {"id": "line", "track": "speech", "start_ms": 28000, "end_ms": 30000, "text": "收好钥匙。"},
            {"id": "key", "track": "visual", "start_ms": 29000, "end_ms": 42000, "text": "把钥匙放在桌上。"},
        ]
        chunks, scenes = timeline(64000, evidence)
        self.assertEqual(chunks[0]["end_ms"], 30000)
        self.assertIn("key", chunks[0]["evidence_ids"])
        self.assertIn("key", chunks[1]["evidence_ids"])
        self.assertEqual(chunks[-1]["end_ms"], 64000)
        self.assertEqual(scenes[0]["evidence_ids"], ["line", "key"])
