"""Shared HTTP and tool types for media search and timeline edits."""

from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import AfterValidator, ConfigDict, Field


class RequestBody(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)


ResourceId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")]
Nonnegative = Annotated[int, Field(ge=0)]
TimeUs = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]
Message = Annotated[str, Field(min_length=1, max_length=12_000)]


class TimeRange(RequestBody):
    start_us: TimeUs
    end_us: TimeUs


class OutputProfile(RequestBody):
    width: Annotated[int, Field(ge=2, le=7680, multiple_of=2)]
    height: Annotated[int, Field(ge=2, le=4320, multiple_of=2)]
    fps_num: Annotated[int, Field(ge=1)]
    fps_den: Annotated[int, Field(ge=1)]
    audio_sample_rate: Literal[48000]


class Scope(RequestBody):
    project_id: ResourceId
    media_revision_ids: NotRequired[Annotated[list[ResourceId], Field(max_length=1000)]]


class SearchQuery(RequestBody):
    scope: Scope
    query_mode: Literal["LOCAL", "FIRST", "ALL", "COUNT"]
    top_k: Annotated[int, Field(ge=1, le=100)]
    text: NotRequired[Message | None]
    image_artifact_id: NotRequired[ResourceId | None]
    range: NotRequired[TimeRange]


def require_query(body: SearchQuery) -> SearchQuery:
    if not (body.get("text") or "").strip() and not body.get("image_artifact_id"):
        raise ValueError("请提供文字或查询图片")
    return body


SearchRequest = Annotated[SearchQuery, AfterValidator(require_query)]
