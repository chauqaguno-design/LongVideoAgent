from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from cutsieve.infrastructure.vector.qdrant import JsonHttpResponse, QdrantVectorError, QdrantVectorIndex


ResponseFactory = Callable[[], object | Awaitable[object]]


class FakeJsonClient:
    def __init__(self, responses: Sequence[object | ResponseFactory] = ()) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        json: Any,
        timeout: float,
    ) -> object:
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "json": json,
                "timeout": timeout,
            }
        )
        if not self.responses:
            raise AssertionError("fake HTTP response queue is empty")
        response = self.responses.pop(0)
        if callable(response):
            response = response()
            if hasattr(response, "__await__"):
                response = await response  # type: ignore[assignment]
        if isinstance(response, BaseException):
            raise response
        return response


def _existing_collection(dimension: int = 2) -> JsonHttpResponse:
    return JsonHttpResponse(
        200,
        {
            "result": {
                "config": {
                    "params": {"vectors": {"size": dimension, "distance": "Cosine"}}
                }
            }
        },
    )


@pytest.mark.parametrize(
    "collection",
    ("", "has.dot", "has/slash", "a" * 129, "中文"),
)
def test_collection_name_must_match_qdrant_safe_pattern(collection: str) -> None:
    with pytest.raises(ValueError):
        QdrantVectorIndex(collection=collection)


@pytest.mark.asyncio
async def test_dimension_mismatch_is_typed_and_invalidates_collection_ready() -> None:
    client = FakeJsonClient([_existing_collection(2)])
    index = QdrantVectorIndex(client=client)

    with pytest.raises(QdrantVectorError):
        await index._ensure_collection(3)
    assert index.collection_ready is False
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_missing_collection_is_created_once():
    client = FakeJsonClient([JsonHttpResponse(404, {}), JsonHttpResponse(200, {})])
    index = QdrantVectorIndex(client=client, collection="scene_evidence", api_key="fixture")
    await asyncio.gather(index._ensure_collection(2), index._ensure_collection(2))
    assert [call["method"] for call in client.calls] == ["GET", "PUT"]
    assert client.calls[1]["url"].endswith("/collections/scene_evidence")
    assert client.calls[1]["json"] == {"vectors": {"size": 2, "distance": "Cosine"}}
    assert client.calls[1]["headers"]["api-key"] == "fixture"
    assert index.collection_ready
