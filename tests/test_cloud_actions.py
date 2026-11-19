import json
import unittest

from examples.describe_clip import source_events


def response(events, finish_reason="stop"):
    content = json.dumps({"events": events})
    return {"choices": [{"message": {"content": content}, "finish_reason": finish_reason}]}


class CloudActionTests(unittest.TestCase):
    def test_each_action_keeps_its_own_interval_on_the_source_timeline(self):
        events = [
            {"start_s": 3.5, "end_s": 4, "action": "右边的人放下杯子。"},
            {"start_s": 1, "end_s": 2.5, "action": "左边的人抬起右手。"},
        ]
        self.assertEqual(source_events(response(events), 5, 600000), [
            {"start_ms": 601000, "end_ms": 602500, "action": "左边的人抬起右手。"},
            {"start_ms": 603500, "end_ms": 604000, "action": "右边的人放下杯子。"},
        ])

    def test_no_visible_action_is_distinct_from_a_bad_response(self):
        self.assertEqual(source_events(response([]), 5, 0), [])
        with self.assertRaises(ValueError):
            source_events(response([], finish_reason="length"), 5, 0)
        with self.assertRaises(ValueError):
            source_events(response([{"start_s": 3, "end_s": 7, "action": "抬头。"}]), 5, 0)
