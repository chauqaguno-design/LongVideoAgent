"""Request bodies accepted by the HTTP API."""

from __future__ import annotations

from typing import Annotated, Literal, NotRequired

from pydantic import Field

from cutsieve.application.edit_timeline import AudioFadeMs

from cutsieve.application.spec_contracts import (
    Message, Nonnegative, OutputProfile, RequestBody, ResourceId, Scope, SearchRequest, TimeUs,
)


Positive = Annotated[int, Field(ge=1)]
Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
IdempotencyKey = Annotated[str, Field(pattern=r"^[A-Za-z0-9._:-]{8,128}$")]


class Idempotent(RequestBody):
    idempotency_key: IdempotencyKey


class ProjectCreate(RequestBody):
    title: Annotated[str, Field(min_length=1, max_length=160)]


class UploadCreate(RequestBody):
    filename: Annotated[str, Field(min_length=1, max_length=255)]
    size_bytes: Positive
    content_type: str
    sha256: NotRequired[Checksum]


class UploadPart(RequestBody):
    part_number: Positive
    etag: str


class UploadComplete(Idempotent):
    parts: list[UploadPart]
    sha256: NotRequired[Checksum]


class UploadRefresh(Idempotent):
    part_numbers: Annotated[list[Positive], Field(min_length=1, max_length=1000)]


class Crop(RequestBody):
    x: Nonnegative
    y: Nonnegative
    width: Positive
    height: Positive


class ClipDraft(RequestBody):
    media_revision_id: ResourceId
    source_start_us: TimeUs
    source_end_us: TimeUs
    audio_policy: Literal["keep", "mute"]
    evidence_ids: list[ResourceId]
    crop: NotRequired[Crop]
    caption_track_id: NotRequired[ResourceId]


class AppendClip(RequestBody):
    op: Literal["append_clip"]
    clip: ClipDraft


class RemoveClip(RequestBody):
    op: Literal["remove_clip"]
    clip_id: ResourceId


class ReplaceClip(RequestBody):
    op: Literal["replace_clip"]
    clip_id: ResourceId
    clip: ClipDraft


class ReorderClips(RequestBody):
    op: Literal["reorder_clips"]
    clip_ids: Annotated[list[ResourceId], Field(min_length=1, max_length=200)]


class SetOutputProfile(RequestBody):
    op: Literal["set_output_profile"]
    output_profile: OutputProfile


class SetCaptionPolicy(RequestBody):
    op: Literal["set_caption_policy"]
    caption_policy: Literal["none", "preserve_source", "burn_aligned"]


class SetAudioFade(RequestBody):
    op: Literal["set_audio_fade"]
    audio_fade_ms: AudioFadeMs


class RestoreRevision(RequestBody):
    op: Literal["restore_revision"]
    source_revision_id: ResourceId


class EditProjectCreate(ProjectCreate, Idempotent):
    project_id: ResourceId
    output_profile: OutputProfile


TimelineOperation = Annotated[
    AppendClip | RemoveClip | ReplaceClip | ReorderClips | SetOutputProfile | SetCaptionPolicy | SetAudioFade
    | RestoreRevision,
    Field(discriminator="op"),
]


class TimelinePatch(Idempotent):
    expected_revision: Nonnegative
    reason: Annotated[str, Field(min_length=1, max_length=2000)]
    operations: Annotated[list[TimelineOperation], Field(min_length=1, max_length=100)]


class RenderJobCreate(Idempotent):
    revision_id: ResourceId
    profile: Literal["PREVIEW", "EXPORT"]


class CancelRequest(Idempotent):
    reason: NotRequired[Annotated[str, Field(max_length=500)]]


class ProcessingCreate(Idempotent):
    requested_capabilities: Annotated[list[
        Literal["asr", "ocr", "visual_observation", "text_embedding", "image_embedding"]
    ], Field(min_length=1, max_length=5)]
    pipeline_version: NotRequired[str]
    force_rebuild: NotRequired[bool]


class Budget(RequestBody):
    max_model_calls: Annotated[int, Field(ge=1, le=100)]
    max_tool_calls: Annotated[int, Field(ge=1, le=500)]
    max_images: Annotated[int, Field(ge=1, le=1000)]
    max_cost_microunits: Nonnegative
    currency: Literal["USD", "CNY"]
    deadline_seconds: Annotated[int, Field(ge=1, le=86400)]


class RunVersion(RequestBody):
    expected_state_version: Nonnegative


class SupersededRun(RunVersion):
    run_id: ResourceId


class WorkflowCreate(Idempotent):
    scope: Scope
    intent: Literal["QUESTION", "INSPECT", "EDIT"]
    message: Message
    edit_project_id: NotRequired[ResourceId]
    budget: NotRequired[Budget]
    supersedes: NotRequired[SupersededRun]


class WorkflowResume(Idempotent, RunVersion):
    message: Message


class BudgetAdjust(Idempotent, RunVersion):
    new_limits: Budget
    reason: Annotated[str, Field(min_length=1, max_length=500)]


class SearchWorkflow(Idempotent):
    search: SearchRequest
