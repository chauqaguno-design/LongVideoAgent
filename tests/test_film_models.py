import json
import unittest

import httpx
from openai import OpenAI

from cinetrace.film_models import FilmModels


class FilmModelTests(unittest.TestCase):
    """Exercise actual SDK serialization and decoding on an offline HTTP transport."""

    def setUp(self):
        self.requests = []
        self.responses = []

        def respond(request):
            self.requests.append(json.loads(request.content))
            return self.responses.pop(0)

        sdk = OpenAI(api_key="test-key", base_url="https://offline.invalid/v1",
                     max_retries=0, http_client=httpx.Client(transport=httpx.MockTransport(respond)))
        self.addCleanup(sdk.close)
        self.models = FilmModels(qwen=sdk, deepseek=sdk)

    def reply(self, content, **fields):
        if isinstance(content, dict):
            content = json.dumps(content, ensure_ascii=False)
        self.responses.append(httpx.Response(200, json={
            "id": "test", "object": "chat.completion", "created": 1, "model": "mock",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": content, **fields}}],
        }))

    def test_research_keeps_only_provider_citations(self):
        source = {"title": "片目", "url": "https://films.example/title"}
        cited = "《试映》中的角色是小安。"
        self.reply(cited + "也提到 https://unverified.example。", annotations=[{
            "type": "url_citation", "url_citation": {**source, "start_index": 0, "end_index": len(cited)},
        }])
        self.reply({"title": "试映", "confirmed": True, "aliases": [], "characters": ["小安"],
                    "events": [], "alternatives": [], "sources": [{"url": "https://fiction.invalid"}]})
        background = self.models.research("试映", ["小安的自我介绍"])
        self.assertEqual(background["sources"], [source])
        self.assertEqual(background["characters"], ["小安"])
        self.assertEqual(self.requests[0]["tools"], [{"type": "deepseek_web_search"}])
        self.assertNotIn("response_format", self.requests[0])
        self.assertNotIn("tools", self.requests[1])
        self.assertNotIn("unverified.example", self.requests[1]["messages"][1]["content"])
        self.assertIn(cited, self.requests[1]["messages"][1]["content"])
        self.assertEqual(self.requests[1]["response_format"], {"type": "json_object"})

    def test_uncited_search_cannot_confirm_a_film(self):
        self.reply("确定是这部片，详见 https://fabricated.example")
        background = self.models.research("片名", [])
        self.assertFalse(background["confirmed"])
        self.assertEqual(background["characters"], [])
        self.assertEqual(background["sources"], [])
        self.assertEqual(len(self.requests), 1)

    def test_multimodal_payloads_and_partial_evidence(self):
        self.reply({"scene": "地下停车场", "people": ["驾驶座上的人"], "objects": ["车"], "actions": []})
        seen = self.models.observe([(1500, b"one"), (4000, b"two")], "叫他过来", {})
        self.assertEqual(seen["scene"], "地下停车场")
        inputs = self.requests[0]["messages"][1]["content"]
        self.assertEqual([item["type"] for item in inputs], ["text", "text", "image_url", "text", "image_url"])
        self.assertIn("1.500", inputs[1]["text"])
        self.assertIn("4.000", inputs[3]["text"])
        self.assertEqual(inputs[2]["image_url"]["url"], "data:image/jpeg;base64,b25l")
        self.reply({"status": "partial", "reason": "能看到车，驾驶者身份不明。", "missing": ["驾驶者身份"]})
        decision = self.models.verify(b"mp4", "找小安驾车的镜头", "叫他过来", {})
        self.assertEqual(decision["status"], "partial")
        self.assertEqual(self.requests[1]["messages"][1]["content"][0], {
            "type": "video_url", "video_url": {"url": "data:video/mp4;base64,bXA0"}, "fps": 4,
        })

    def test_event_partition_must_cover_every_window_in_order(self):
        self.reply({"events": [{"first": 0, "last": 0, "description": "上车"}]})
        with self.assertRaisesRegex(ValueError, "遗漏"):
            self.models.organize([{"scene": "上车"}, {"scene": "开车"}])
        event = {"first": 0, "last": 1, "description": "先后上车开车"}
        self.reply({"events": [event]})
        self.assertEqual(self.models.organize([{"scene": "上车"}, {"scene": "开车"}]), [event])

    def test_no_web_for_query_expansion_and_summary(self):
        self.reply({"terms": ["赛车", "赛车", "弯道"]})
        self.assertEqual(self.models.expand("在哪里飙车？", {}, ["路况"]), ["赛车", "弯道"])
        self.assertIn("路况", self.requests[0]["messages"][1]["content"])
        self.reply("车辆通过弯道。")
        self.assertEqual(self.models.summarize([{"description": "车过弯"}]), "车辆通过弯道。")
        self.assertTrue(all("tools" not in body for body in self.requests))

    def test_api_error_does_not_echo_response_body(self):
        self.responses.append(httpx.Response(401, json={"error": {"message": "secret-value"}}))
        with self.assertRaisesRegex(RuntimeError, "HTTP 401") as caught:
            self.models.expand("测试", {}, [])
        self.assertNotIn("secret-value", str(caught.exception))
