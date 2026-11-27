"""Request bodies accepted by the HTTP API."""

from __future__ import annotations

import re
from typing import Annotated, Literal, NotRequired

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cutsieve.application.spec_contracts import (
    Message, Nonnegative, RequestBody, ResourceId, Scope, SearchRequest, TimeUs,
)
from cutsieve.application.workflow_checkpoint import OutputProfile


class ApiModel(BaseModel):
    """Strict HTTP model with camelCase aliases."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        serialize_by_alias=True,
    )

class RegisterRequest(ApiModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
            raise ValueError("账号需为 3-32 位字母、数字或下划线")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not 8 <= len(value) <= 128:
            raise ValueError("密码需为 8-128 位")
        return value

    @field_validator("nickname")
    @classmethod
    def validate_nickname(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) > 50:
            raise ValueError("昵称不能超过 50 个字符")
        return value

class LoginRequest(ApiModel):
    username: str
    password: str
    nickname: str | None = None

    @field_validator("username")
    @classmethod
    def validate_username(cls, value: str) -> str:
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
            raise ValueError("请输入有效账号")
        return value

    @field_validator("password")
    @classmethod
    def validate_password(cls, value: str) -> str:
        if not isinstance(value, str) or not value.strip() or len(value) > 128:
            raise ValueError("请输入有效密码")
        return value

class UserInfo(ApiModel):
    id: int
    username: str
    nickname: str
    avatar: str | None = None
    role: str = "USER"

class AuthData(ApiModel):
    user_info: Annotated[UserInfo, Field(alias="userInfo")]
    token: str | None = None


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


class RestoreRevision(RequestBody):
    op: Literal["restore_revision"]
    source_revision_id: ResourceId


class EditProjectCreate(ProjectCreate, Idempotent):
    project_id: ResourceId
    output_profile: OutputProfile


TimelineOperation = Annotated[
    AppendClip | RemoveClip | ReplaceClip | ReorderClips | SetOutputProfile | SetCaptionPolicy | RestoreRevision,
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
