"""Durable state for one agent turn, stored in the workflow run's JSON column."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

type Document = dict[str, JsonValue]


class RunScope(BaseModel, frozen=True):
    project_id: str
    media_revision_ids: tuple[str, ...]


class RunSpec(BaseModel, frozen=True):
    scope: RunScope
    message: str
    parent_run_id: str | None = None
    media_revision_id: str | None = None
    image_artifact_id: str | None = None
    search_request: Document | None = None


class Usage(BaseModel):
    model_calls: int = 0
    tool_calls: int = 0
    images: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_cny: float = 0.0
    max_steps: int = 15
    max_images: int = 24
    images_unloaded: int = 0
    compactions: int = 0


class ToolStep(BaseModel):
    tool_call_id: str
    name: str
    summary: str
    status: Literal["RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"] = "RUNNING"
    ok: bool | None = None
    args_preview: str = ""
    duration_ms: int | None = None
    error_code: str | None = None
    repaired: bool = False
    images_added: int = 0
    step_index: int | None = None


class TraceEvent(BaseModel):
    kind: Literal["MODEL_TURN", "IMAGE_UNLOAD", "COMPACTION", "SELF_REPAIR", "RENDER_WAIT"]
    step: int = 0
    summary: str
    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int | None = None


class Citation(BaseModel):
    media_revision_id: str
    asset_id: str
    start_us: int
    end_us: int
    label: str
    text: str
    source: str


class Clarification(BaseModel):
    question: str
    reply: str


class PendingRender(BaseModel):
    tool_call_id: str
    render_job_id: str
    profile: Literal["PREVIEW", "EXPORT"]
    edit_project_id: str
    revision_id: str
    started_at_ms: int | None = None


class PendingAsk(BaseModel):
    tool_call_id: str
    question: str
    options: list[str]


class Checkpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    spec: RunSpec
    messages: list[Document] = Field(default_factory=list)
    tool_steps: list[ToolStep] = Field(default_factory=list)
    trace_events: list[TraceEvent] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)
    pending_render_tool_call: PendingRender | None = None
    pending_ask_tool_call: PendingAsk | None = None
    usage: Usage = Field(default_factory=Usage)
    warnings: list[str] = Field(default_factory=list)
    edit_project_id: str | None = None
    revision_id: str | None = None
    preview_artifact_id: str | None = None
    export_artifact_id: str | None = None
    timeline_heads: dict[str, int] = Field(default_factory=dict)
    render_job_ids: list[str] = Field(default_factory=list)
    related_media_revision_ids: list[str] = Field(default_factory=list)
    paused_at: int | None = None
    retry_replies: list[str] = Field(default_factory=list)
    lease_failures: int = 0

    @model_validator(mode="before")
    @classmethod
    def read_legacy_checkpoint(cls, value):
        """Preserve immutable outputs and conversation links from existing databases."""
        if not isinstance(value, dict) or "attempt" not in value:
            return value
        spec, attempt = value["spec"], value["attempt"]
        origin = spec.get("conversation") or {}
        media = spec["scope"]["media_revision_ids"]
        edit_id = attempt.get("edit_id")
        return dict(
            spec=dict(
                scope=spec["scope"],
                message=spec["message"],
                parent_run_id=origin.get("parent_run_id"),
                media_revision_id=media[0] if len(media) == 1 else None,
                image_artifact_id=(spec.get("search_request") or {}).get("image_artifact_id"),
                search_request=spec.get("search_request"),
            ),
            edit_project_id=edit_id,
            revision_id=attempt.get("revision_id") or attempt.get("base_id"),
            preview_artifact_id=attempt.get("preview_artifact_id"),
            export_artifact_id=attempt.get("export_artifact_id"),
            clarifications=attempt.get("clarifications", []),
            render_job_ids=[job for job in (attempt.get("render_job_id"), attempt.get("export_job_id")) if job],
            lease_failures=attempt.get("lease_failures", 0),
        )

    @property
    def protected_media(self):
        return (*self.spec.scope.media_revision_ids, *self.related_media_revision_ids)

    def remember_media(self, media_ids):
        self.related_media_revision_ids = list(dict.fromkeys([*self.related_media_revision_ids, *media_ids]))

    def set_timeline(self, edit_id, revision_id, revision_no):
        if (edit_id, revision_id) != (self.edit_project_id, self.revision_id):
            self.preview_artifact_id = self.export_artifact_id = None
        self.edit_project_id = edit_id
        self.revision_id = revision_id
        self.timeline_heads[edit_id] = revision_no

    def finish_tool(self, call_id, content, summary, ok, *, duration_ms=None, error_code=None, images_added=0):
        step = next(item for item in self.tool_steps if item.tool_call_id == call_id)
        step.repaired = ok and next(
            (other.ok is False for other in reversed(self.tool_steps) if other.name == step.name and other.ok is not None),
            False,
        )
        if step.repaired:
            self.trace_events.append(TraceEvent(
                kind="SELF_REPAIR", step=step.step_index or 0, summary=f"{step.name} succeeded on retry.",
            ))
        step.summary = summary
        step.status = "SUCCEEDED" if ok else "FAILED"
        step.ok = ok
        step.duration_ms = duration_ms
        step.error_code = error_code
        step.images_added = images_added
        self.messages.append(dict(role="tool", tool_call_id=call_id, content=content))
        return step.model_dump()

    def result(self, answer):
        return dict(
            answer=answer,
            citations=[citation.model_dump() for citation in self.citations],
            tool_steps=[step.model_dump() for step in self.tool_steps],
            edit_project_id=self.edit_project_id,
            revision_id=self.revision_id,
            preview_artifact_id=self.preview_artifact_id,
            export_artifact_id=self.export_artifact_id,
            warnings=self.warnings,
        )
