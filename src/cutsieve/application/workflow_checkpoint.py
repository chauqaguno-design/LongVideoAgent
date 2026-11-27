"""The persisted inputs, current plan, authorized references and repair lineage of one run."""

from dataclasses import dataclass
from typing import Annotated, Literal, NotRequired, TypedDict

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from .edit_timeline import EditError
from .spec_contracts import RequestBody, TimeUs


type Document = dict[str, JsonValue]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True, revalidate_instances="always")


class RunScope(Record, frozen=True):
    project_id: str
    media_revision_ids: tuple[str, ...]


class ModelIdentity(Record, frozen=True):
    model: str
    fingerprint: str
    mode: Literal["native", "json"]


class OutputProfile(RequestBody):
    width: Annotated[int, Field(ge=2, le=7680, multiple_of=2)]
    height: Annotated[int, Field(ge=2, le=4320, multiple_of=2)]
    fps_num: Annotated[int, Field(ge=1)]
    fps_den: Annotated[int, Field(ge=1)]
    audio_sample_rate: Literal[48000]


class EditBrief(Record, frozen=True):
    brief_id: str
    project_id: str
    goal: str
    source_media_revision_ids: tuple[str, ...]
    selection_mode: Literal["EXPLICIT_RANGES", "SEMANTIC"]
    duration_target_us: TimeUs | None
    duration_tolerance_us: TimeUs
    order_policy: Literal["SOURCE_ORDER", "USER_ORDER", "NARRATIVE"]
    audio_policy: Literal["KEEP_SOURCE", "MUTE"]
    must_include: tuple[str, ...]
    must_exclude: tuple[str, ...]
    output_profile: OutputProfile


class ConversationOrigin(Record, frozen=True):
    parent_run_id: str | None = None


class RunSpec(Record, frozen=True):
    scope: RunScope
    message: str
    created: int
    model_identity: ModelIdentity | None = None
    index_generation_id: str | None = None
    brief: EditBrief | None = None
    stage_model_limit: int | None = None
    conversation: ConversationOrigin | None = None
    search_request: Document | None = None

    def pin_index(self, generation: str):
        if self.index_generation_id not in (None, generation):
            raise EditError(409, "INDEX_CHANGED", "素材索引已固定")
        return self.model_copy(update={"index_generation_id": generation})

    def pin_model(self, identity: ModelIdentity):
        if self.model_identity not in (None, identity):
            raise EditError(409, "MODEL_CHANGED", "原运行固定的模型版本或协议已改变")
        return self.model_copy(update={"model_identity": identity})

    def bind_brief(self, brief: EditBrief):
        if self.brief not in (None, brief):
            raise EditError(409, "BRIEF_CHANGED", "当前计划的剪辑约束已固定")
        return self.model_copy(update={"brief": brief})


class TimeRange(TypedDict):
    start_us: int
    end_us: int


type Interval = tuple[str, TimeRange, list[str]]


class Candidate(TypedDict):
    candidate_id: str
    media_revision_id: str
    range: TimeRange
    evidence_ids: list[str]
    support: Literal["SUPPORTED", "UNCERTAIN", "CONFLICTING"]
    reason: str


class Proposal(TypedDict):
    base_revision: int
    operations: list[Document]
    brief_id: str


class Clarification(TypedDict):
    question: str
    reply: str


class PlanAttempt(Record):
    turn: int = Field(default=0, ge=0)
    plan_generation: int = Field(default=0, ge=0)
    intent: Literal["AUTO", "QUESTION", "INSPECT", "EDIT"] = "AUTO"
    route_decision: Document | None = None
    clarification: str | None = None
    clarifications: list[Clarification] = Field(default_factory=list)
    selected_candidate: Document | None = None
    conversation_candidates: list[Document] | None = None
    parent_brief: Document | None = None
    parent_selection: str | None = None
    edit_id: str | None = None
    base: int = 0
    base_id: str | None = None
    dialogs: list[Document] = Field(default_factory=list)
    native_call_ids: list[str] = Field(default_factory=list)
    model_round: int = 0
    format_repaired: bool = False
    skill_snapshots: dict[str, Document] = Field(default_factory=dict)
    plan: Document = Field(default_factory=dict)
    brief_id: str | None = None
    intervals: list[Interval] = Field(default_factory=list)
    target_clip_ids: list[str] = Field(default_factory=list)
    preserved_clip_ids: list[str] = Field(default_factory=list)
    original_clip: Document | None = None
    candidate_snapshot: list[Candidate] = Field(default_factory=list)
    candidates_id: str | None = None
    shot_plan: Document | None = None
    shot_plan_id: str | None = None
    proposal: Proposal | None = None
    proposal_id: str | None = None
    revision_id: str | None = None
    timeline_change: Document | None = None
    render_job_id: str | None = None
    preview_artifact_id: str | None = None
    qa_id: str | None = None
    preview_accepted: bool = False
    preview_review_id: str | None = None
    export_requested: bool = False
    export_format: str = "mp4"
    export_job_id: str | None = None
    export_artifact_id: str | None = None
    export_qa_id: str | None = None
    export_review_id: str | None = None
    answer: Document | None = None
    lease_owner: str | None = None
    lease_failures: int = 0

    def next_attempt(self):
        """Only the user's active selection, conversation and edit base carry forward."""
        return PlanAttempt(
            plan_generation=self.plan_generation + 1, turn=self.turn, intent=self.intent,
            clarification=self.clarification, clarifications=self.clarifications,
            route_decision=self.route_decision, selected_candidate=self.selected_candidate,
            parent_brief=self.parent_brief, parent_selection=self.parent_selection,
            edit_id=self.edit_id, base=self.base, base_id=self.base_id,
        )

    def after_clarification(self, message: str):
        new = self.next_attempt()
        new.turn += 1
        new.clarification = message
        return new

    def for_repair(self, *, base: int, base_id: str, original: Document,
                   interval: Interval, preserved: list[str]):
        new = self.next_attempt()
        new.base, new.base_id = base, base_id
        new.plan, new.brief_id = self.plan, self.brief_id
        new.candidate_snapshot, new.candidates_id = self.candidate_snapshot, self.candidates_id
        new.export_requested, new.export_format = self.export_requested, self.export_format
        new.original_clip, new.intervals = original, [interval]
        new.target_clip_ids, new.preserved_clip_ids = [original["clip_id"]], preserved
        return new


class ContextReference(TypedDict):
    type: Literal["EVIDENCE", "TIMELINE", "ARTIFACT"]
    id: str
    produced_by: str
    media_revision_id: NotRequired[str]
    source_generation_id: NotRequired[str | None]
    index_generation_id: NotRequired[str | None]
    timeline_revision_id: NotRequired[str | None]
    range: NotRequired[TimeRange]


class ContextManifest(Record):
    references: dict[str, ContextReference] = Field(default_factory=dict)
    # Job IDs stay readable after their originating plan has been replaced.
    inspections: dict[str, Document] = Field(default_factory=dict)
    processing_job_ids: list[str] = Field(default_factory=list)


class RepairRecord(TypedDict):
    plan_generation: int
    review_report_id: str
    shot_plan_id: str
    proposal_id: str
    revision_id: str
    preview_artifact_id: str


class RepairHistory(Record):
    repair_count: int = Field(default=0, ge=0)
    repair_limit: int = Field(default=2, ge=0, le=2)
    signatures: list[str] = Field(default_factory=list)
    rejected_candidates: list[str] = Field(default_factory=list)
    lineage: list[RepairRecord] = Field(default_factory=list)


@dataclass(frozen=True)
class ClipRepair:
    signature: str
    attempt: PlanAttempt
    rejected_candidates: list[str]


class Checkpoint(Record):
    spec: RunSpec
    attempt: PlanAttempt
    context: ContextManifest
    repair: RepairHistory
