"""Read tools exposed to the planner and their input boundaries."""

from dataclasses import dataclass
from functools import cache
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import AfterValidator, ConfigDict, Field, TypeAdapter


class RequestBody(TypedDict):
    __pydantic_config__ = ConfigDict(extra="forbid", strict=True)


ResourceId = Annotated[str, Field(pattern=r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")]
Nonnegative = Annotated[int, Field(ge=0)]
TimeUs = Annotated[int, Field(strict=True, ge=0, le=9_007_199_254_740_991)]
Message = Annotated[str, Field(min_length=1, max_length=12_000)]


class TimeRange(RequestBody):
    start_us: TimeUs
    end_us: TimeUs


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


class SegmentArgs(RequestBody):
    media_revision_id: ResourceId
    range: TimeRange


class ContextArgs(RequestBody):
    evidence_id: ResourceId
    before_us: TimeUs
    after_us: TimeUs
    offset: NotRequired[Nonnegative]


class CoverageArgs(SegmentArgs):
    query_run_id: ResourceId
    protocol_id: str


class RevisionArgs(RequestBody):
    revision_id: ResourceId


class TimelineArgs(RevisionArgs):
    edit_project_id: ResourceId


class JobArgs(RequestBody):
    job_id: ResourceId


@dataclass(frozen=True)
class ToolContract:
    name: str
    phases: tuple[str, ...]
    args: TypeAdapter
    description: str

    @property
    def provider_name(self):
        return self.name.replace(".", "__")

    def validate(self, value):
        self.args.validate_python(value)

    def definition(self):
        return {"type": "function", "function": {
            "name": self.provider_name, "description": self.description, "parameters": self.args.json_schema(),
        }}


class ToolCatalog:
    def __init__(self, entries):
        self.tools = {name: ToolContract(name, phases, TypeAdapter(args), description)
                      for name, phases, args, description in entries}
        self.by_provider = {tool.provider_name: tool for tool in self.tools.values()}

    def for_provider_call(self, wire, phase):
        tool = self.by_provider.get(wire)
        if tool is None or phase not in tool.phases:
            raise ValueError("Unknown or unauthorized read tool")
        return tool

    def provider_tools(self, phase):
        return [tool.definition() for tool in self.tools.values() if phase in tool.phases]


@cache
def tool_catalog():
    search = ("PLAN", "MATERIAL")
    return ToolCatalog((
        ("video.search_evidence", search, SearchRequest, "Find candidate evidence in the authorized scope."),
        ("video.get_segment", search, SegmentArgs, "Read evidence references for a source interval."),
        ("video.get_context_window", (*search, "PROPOSE", "REVIEW"), ContextArgs, "Read around a known evidence ID."),
        ("video.check_coverage", search, CoverageArgs, "Check the requested interval for unverified ranges."),
        ("audio.inspect_transcript", search, SegmentArgs, "Read transcript references and quality flags."),
        ("edit.get_timeline", ("PLAN", "PROPOSE", "REVIEW"), TimelineArgs, "Read a bound timeline revision."),
        ("edit.validate_timeline", ("PLAN", "PROPOSE"), RevisionArgs, "Check a bound timeline revision."),
        ("jobs.get", (*search, "REVIEW"), JobArgs, "Read the status of a job belonging to this run."),
    ))


def validate_contract(name, body):
    """Review report check used by the review stage."""
    if name != "ReviewReport" or body.get("output_kind") not in ("PREVIEW", "EXPORT"):
        raise ValueError("Invalid review report")
