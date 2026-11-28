"""The eight native tools and their project-scoped adapters."""

import json
import subprocess
from base64 import b64encode
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from cutsieve.infrastructure.edit_media import US, media_binaries, probe_media
from cutsieve.infrastructure.media.process import MediaStopped
from cutsieve.infrastructure.media.source_artifacts import SourceReader
from cutsieve.infrastructure.persistence.edit_repository import one, owned
from cutsieve.infrastructure.persistence.edit_tables import projects, revisions
from cutsieve.infrastructure.persistence.processing_tables import runs as processing_runs
from cutsieve.infrastructure.persistence.source_tables import sources
from cutsieve.infrastructure.persistence.workflow_repository import StaleLease

from .edit_sources import asset_view
from .edit_timeline import EditError, missing
from .evidence_search import subtitle_literal
from .spec_contracts import Nonnegative, ResourceId, TimeUs
from .workflow_checkpoint import Citation, PendingAsk, PendingRender
from .workflow_state import stable_id


Seconds = Annotated[float, Field(ge=0, le=9_007_199_254)]
CaptionPolicy = Literal["none", "preserve_source", "burn_aligned"]
DEFAULT_PROFILE = dict(width=1280, height=720, fps_num=25, fps_den=1, audio_sample_rate=48000)
RENDER_TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED"}


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False, str_strip_whitespace=True)


class MediaOverview(Arguments):
    asset_id: ResourceId | None = None


class EvidenceQuery(MediaOverview):
    query: Annotated[str, Field(max_length=12000)] | None = None
    mode: Literal["LOCAL", "FIRST", "ALL", "COUNT"] = "LOCAL"
    image_artifact_id: ResourceId | None = None
    top_k: Annotated[int, Field(ge=1, le=100)] = 10


class Transcript(Arguments):
    asset_id: ResourceId
    start_s: Seconds
    end_s: Seconds
    offset: Nonnegative = 0


class Frames(MediaOverview):
    artifact_id: ResourceId | None = None
    timestamps_s: Annotated[list[Seconds], Field(min_length=1, max_length=6)] | None = None
    start_s: Seconds | None = None
    end_s: Seconds | None = None
    count: Annotated[int, Field(ge=1, le=6)] | None = None


class EditTarget(Arguments):
    edit_project_id: ResourceId | None = None


class TimelineQuery(EditTarget):
    offset: Nonnegative = 0


class Crop(Arguments):
    x: Nonnegative
    y: Nonnegative
    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]


class ClipDraft(MediaOverview):
    media_revision_id: ResourceId | None = None
    start_s: Seconds | None = None
    end_s: Seconds | None = None
    source_start_us: TimeUs | None = None
    source_end_us: TimeUs | None = None
    audio_policy: Literal["keep", "mute"] = "keep"
    evidence_ids: list[ResourceId] = Field(default_factory=list, max_length=100)
    crop: Crop | None = None
    caption_track_id: ResourceId | None = None


class AppendClip(Arguments):
    op: Literal["append_clip"]
    clip: ClipDraft


class RemoveClip(Arguments):
    op: Literal["remove_clip"]
    clip_id: ResourceId


class ReplaceClip(RemoveClip):
    op: Literal["replace_clip"]
    clip: ClipDraft


class ReorderClips(Arguments):
    op: Literal["reorder_clips"]
    clip_ids: Annotated[list[ResourceId], Field(min_length=1, max_length=200)]


class SetCaptionPolicy(Arguments):
    op: Literal["set_caption_policy"]
    caption_policy: CaptionPolicy


class RestoreRevision(Arguments):
    op: Literal["restore_revision"]
    source_revision_id: ResourceId | None = None
    revision_no: Nonnegative | None = None


Operation = Annotated[
    AppendClip | ReplaceClip | RemoveClip | ReorderClips | SetCaptionPolicy | RestoreRevision,
    Field(discriminator="op"),
]


class TimelineEdit(EditTarget):
    clips: Annotated[list[ClipDraft], Field(max_length=200)] | None = None
    operations: Annotated[list[Operation], Field(min_length=1, max_length=100)] | None = None
    caption_policy: CaptionPolicy | None = None
    title: Annotated[str, Field(min_length=1, max_length=160)] | None = None
    expected_revision: Nonnegative | None = None


class Render(EditTarget):
    profile: Literal["PREVIEW", "EXPORT"] = "PREVIEW"
    format: Literal["mp4", "webm", "mkv"] = "mp4"


class Ask(Arguments):
    question: Annotated[str, Field(min_length=1, max_length=1000)]
    options: Annotated[list[Annotated[str, Field(min_length=1, max_length=300)]], Field(min_length=1, max_length=12)]


TOOLS = {
    "get_media_overview": (MediaOverview, "List scoped assets, processing, available evidence and viewing notes."),
    "search_evidence": (EvidenceQuery, "Find timestamped subtitle, ASR, OCR and visual evidence by text or query image. "
                        "FIRST requires a single asset. Distinguish candidates from verified occurrences."),
    "read_transcript": (Transcript, "Read aligned subtitles, ASR, OCR and visual observations in source seconds. "
                        "Use the returned next arguments for more rows or the next 180-second window."),
    "inspect_frames": (Frames, "Read 1 to 6 JPEG frames from one source asset or one rendered artifact. "
                       "Choose explicit timestamps or an interval and count, in seconds."),
    "get_timeline": (TimelineQuery, "Read the active or latest timeline, clip IDs and its optimistic head revision."),
    "edit_timeline": (TimelineEdit, "Replace all clips with clips, or apply operations, in one revision. "
                      "A clip uses start_s and end_s (or source_start_us and source_end_us) and its source asset "
                      "or media revision. Read the timeline again if its expected revision conflicts."),
    "render_video": (Render, "Render the current non-empty timeline. PREVIEW uses mp4. "
                     "EXPORT supports mp4, webm and mkv. Returns when the job finishes."),
    "ask_user": (Ask, "Pause only when a decision genuinely needs user input. Offer concrete selectable options."),
}
TOOL_DEFINITIONS = [
    dict(type="function", function=dict(name=name, description=description, parameters=schema.model_json_schema()))
    for name, (schema, description) in TOOLS.items()
]
TRANSACTION_TOOLS = {"get_timeline", "edit_timeline", "render_video", "ask_user"}


@dataclass
class ToolResult:
    body: dict
    citations: list[Citation] = field(default_factory=list)
    images: list[dict] = field(default_factory=list)
    notices: list[tuple[str, dict]] = field(default_factory=list)
    related_media: list[str] = field(default_factory=list)


def tool_error(code, message, hint, **details):
    return ToolResult(dict(ok=False, code=code, message=message, hint=hint, **details))


def image_part(mime, data):
    return dict(type="image_url", image_url=dict(url=f"data:{mime};base64," + b64encode(data).decode("ascii")))


def media_summary(row):
    asset, probe = asset_view(row), row.probe
    caps = asset["capabilities"]
    return dict(
        asset_id=row.id, media_revision_id=row.revision, filename=row.filename,
        duration_s=probe["duration_us"] / US, status=asset["status"],
        has_subtitles=bool(probe.get("has_subtitles")),
        has_visual_observations=any(c["name"] == "visual_observation" and c["state"] == "AVAILABLE" for c in caps),
    )


def citation(asset, start, end, text, source):
    return Citation(
        media_revision_id=asset.revision, asset_id=asset.id, start_us=start, end_us=end,
        label=f"{asset.filename} · {start / US:.6f}s", text=text[:800], source=source,
    )


def evidence_item(row, asset):
    start, end = row["range"]["start_us"], row["range"]["end_us"]
    proof = citation(asset, start, end, row["claim"], row["source"])
    hit = dict(proof.model_dump(exclude={"label", "text"}), start_s=start / US, end_s=end / US,
               text=row["claim"], evidence_id=row["evidence_id"], support=row["support"])
    return hit, proof


class FrameStop:
    def __init__(self, repo, claim):
        self.repo, self.claim = repo, claim

    def is_set(self):
        with self.repo.sessions() as db:
            try:
                self.repo.guard(db, self.claim, lock=False)
            except StaleLease:
                return True
        return False


class AgentTools:
    def __init__(self, repo, search, render_worker):
        self.repo, self.search, self.render_worker = repo, search, render_worker

    def assets(self, row, *, db=None):
        if db is None:
            with self.repo.sessions() as db:
                return self.assets(row, db=db)
        return db.execute(
            select(sources).where(
                sources.c.workspace == row.workspace_id, sources.c.project == row.project_id,
                sources.c.revision.in_(row.checkpoint.spec.scope.media_revision_ids),
            ).order_by(sources.c.filename, sources.c.id)
        ).all()

    @staticmethod
    def asset(assets, asset_id=None, media_revision_id=None):
        if not asset_id and not media_revision_id and len(assets) != 1:
            raise EditError(422, "ASSET_REQUIRED", "Select one available asset by asset_id or media_revision_id.")
        for asset in assets:
            if (not asset_id or asset.id == asset_id) and (not media_revision_id or asset.revision == media_revision_id):
                return asset
        raise EditError(404, "ASSET_NOT_FOUND", "The requested asset and media revision are not available in this turn.")

    def query_image(self, row):
        workspace, project, key = row.workspace_id, row.project_id, row.checkpoint.spec.image_artifact_id
        image = self.search.files.get(workspace, key, project=project)
        if image is None or image.kind != "IMAGE_QUERY":
            raise EditError(404, "IMAGE_NOT_FOUND", "Upload a query image to this project and start a turn with its ID.")
        return image_part(image.mime, self.search.files.path(image).read_bytes())

    def get_media_overview(self, row, args):
        assets = self.assets(row)
        if args.asset_id:
            assets = [self.asset(assets, args.asset_id)]
        items = []
        with self.repo.sessions() as db:
            statuses = dict(db.execute(
                select(processing_runs.c.asset, processing_runs.c.status).where(
                    processing_runs.c.workspace == row.workspace_id, processing_runs.c.kind == "INDEX",
                    processing_runs.c.asset.in_([a.id for a in assets]),
                ).order_by(processing_runs.c.created, processing_runs.c.id)
            ).all())
        for asset in assets:
            caps = (asset.probe or {}).get("capabilities", [])
            notes = self.viewing_notes(row.workspace_id, asset.probe.get("notes_artifact_id"))
            items.append(dict(
                media_summary(asset), capabilities=caps,
                processing_status=statuses.get(asset.id, "NOT_INDEXED"), viewing_notes=notes,
            ))
        return ToolResult(dict(ok=True, assets=items))

    def viewing_notes(self, workspace, key):
        if not key or not (record := self.search.catalog.get_record(workspace, key, "ARTIFACT")):
            return None
        with self.search.catalog.derived_path(record.body["file"]).open(encoding="utf-8") as stream:
            notes = json.load(stream)
        overview = notes["overview"]
        return dict(
            artifact_id=key,
            overview=dict(chapter_count=overview["chapter_count"], highlights=[
                dict(text=claim["text"][:250], range=claim["range"], source=claim["source"])
                for claim in overview["highlights"]
            ]),
            chapters=[
                dict(start_s=chapter["start_us"] / US, end_s=chapter["end_us"] / US,
                     observation_count=len(chapter["claims"]),
                     summary=[claim["text"][:250] for claim in chapter["claims"][:3]])
                for chapter in notes["chapters"]
            ],
        )

    def search_evidence(self, row, args, call_id):
        if not args.query and not args.image_artifact_id:
            raise EditError(422, "EMPTY_QUERY", "Provide a text query, an image_artifact_id, or both.")
        assets = self.assets(row)
        if args.asset_id:
            assets = [self.asset(assets, args.asset_id)]
        if args.mode == "FIRST" and len(assets) > 1:
            raise EditError(422, "SEARCH_SCOPE", "FIRST needs one asset. Provide its asset_id.")
        if not assets:
            return tool_error("NO_EVIDENCE", "There are no available assets in this turn.",
                              "Use get_media_overview to check the available sources.")
        workspace = row.workspace_id
        by_media = {asset.revision: asset for asset in assets}
        request = dict(
            scope=dict(project_id=row.project_id, media_revision_ids=list(by_media)),
            text=args.query, image_artifact_id=args.image_artifact_id, query_mode=args.mode, top_k=args.top_k,
        )
        plan = self.search.plan(workspace, request)
        ranked = self.search.rank(workspace, request, plan)
        self.search.recheck_scope(workspace, plan)
        ordered = [key for hit in ranked["hits"] for key in hit["evidence_ids"]]
        body = dict(ok=True, mode=args.mode, warnings=ranked["warnings"], index_generation_id=plan.generation,
                    candidate_count=ranked["candidate_count"], truncated=ranked["truncated"])
        if args.mode != "LOCAL":
            ordered = self.verified_matches(row, call_id, request, plan, ranked, ordered, body)
        if not ordered:
            return tool_error("NO_EVIDENCE", "No indexed evidence matched the query.",
                              "Try a shorter phrase, other wording, or inspect a relevant source interval.",
                              **{key: value for key, value in body.items() if key != "ok"})
        by_id = {item["evidence_id"]: item for item in plan.rows}
        pairs = [evidence_item(by_id[key], by_media[by_id[key]["media_revision_id"]])
                 for key in dict.fromkeys(ordered) if key in by_id]
        pairs = pairs[:args.top_k]
        return ToolResult(dict(body, hits=[hit for hit, _ in pairs]), [proof for _, proof in pairs])

    def verified_matches(self, row, call_id, request, plan, ranked, ordered, body):
        workspace = row.workspace_id
        checked = {}
        if subtitle_literal(request):
            media = [item["media_revision_id"] for item in plan.requested]
            checked = self.search.catalog.literal_coverage(workspace, row.project_id, plan.generation, media)
        scan_id = stable_id(row.id, f"evidence:{call_id}:{plan.fingerprint}")
        evaluation = self.search.ground(workspace, scan_id, request, plan, ranked, checked)
        artifact = evaluation["complete_event_list_artifact_id"]
        occurrences = self.search.files.occurrences(workspace, row.project_id, artifact)
        occurrences.sort(key=lambda event: event["range"]["start_us"])
        body.update(
            found_event_count=evaluation["found_event_count"], count_qualifier=evaluation["count_qualifier"],
            complete_event_list_artifact_id=artifact, warnings=evaluation["warnings"],
        )
        proven = [key for event in occurrences for key in event["evidence_ids"]]
        if request["query_mode"] == "FIRST":
            if occurrences:
                return occurrences[0]["evidence_ids"]
            ranges = {item["evidence_id"]: item["range"]["start_us"] for item in plan.rows}
            return sorted(ordered, key=ranges.__getitem__)
        return proven + ordered

    def read_transcript(self, row, args):
        asset = self.asset(self.assets(row), args.asset_id)
        start, requested_end = round(args.start_s * US), round(args.end_s * US)
        end = min(requested_end, asset.probe["duration_us"])
        if not start < end:
            raise EditError(422, "INVALID_RANGE", "The transcript interval must overlap the source duration.")
        stop = min(end, start + 180 * US)
        _, indexed = self.search.catalog.search_scope(
            row.workspace_id, row.project_id, [asset.revision], span=dict(start_us=start, end_us=stop)
        )
        aligned = sorted((r for r in indexed if r["source"] != "sampled-frame"),
                         key=lambda r: (r["range"]["start_us"], r["range"]["end_us"], r["evidence_id"]))
        limit = 20
        page = aligned[args.offset:args.offset + limit]
        pairs = [evidence_item(item, asset) for item in page]
        next_page = None
        if args.offset + limit < len(aligned):
            next_page = dict(asset_id=asset.id, start_s=start / US, end_s=end / US, offset=args.offset + limit)
        elif stop < end:
            next_page = dict(asset_id=asset.id, start_s=stop / US, end_s=end / US)
        return ToolResult(
            dict(ok=True, asset_id=asset.id, media_revision_id=asset.revision, start_s=start / US, end_s=stop / US,
                 rows=[hit for hit, _ in pairs], next=next_page),
            [proof for _, proof in pairs],
        )

    def inspect_frames(self, row, args):
        if args.asset_id and args.artifact_id:
            raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Specify one asset_id or one artifact_id.")
        asset, related = None, []
        workspace = row.workspace_id
        if args.artifact_id:
            artifact = self.render_worker.artifact_record(workspace, args.artifact_id)
            if artifact.kind not in ("PREVIEW", "EXPORT"):
                raise EditError(422, "NOT_A_VIDEO", "Select a rendered PREVIEW or EXPORT video artifact.")
            with self.repo.sessions() as db:
                revision = one(db, owned(revisions, workspace, artifact.revision_id))
                one(db, owned(projects, workspace, revision.edit_id).where(projects.c.project_id == row.project_id))
            related = [c["media_revision_id"] for c in revision.snapshot["clips"]]
            path = self.render_worker.materialize(artifact)
            target = dict(artifact_id=artifact.id, edit_project_id=revision.edit_id, revision_id=revision.id)
        else:
            asset = self.asset(self.assets(row), args.asset_id)
            path = self.repo.sources.source_path(workspace, asset.revision)
            target = dict(asset_id=asset.id, media_revision_id=asset.revision)
        remaining = row.checkpoint.usage.max_images - row.checkpoint.usage.images
        requested = len(args.timestamps_s) if args.timestamps_s else (args.count or 3)
        if requested > remaining:
            return tool_error(
                "MAX_IMAGES_EXCEEDED", "The requested frames exceed the remaining image allowance.",
                "Request fewer frames or use available transcript and timeline evidence.", remaining_images=remaining,
            )
        tools = media_binaries()
        stopped = FrameStop(self.repo, row)
        try:
            meta = asset.probe if asset else probe_media(path, ffprobe=tools["ffprobe"], stopped=stopped)
            times = frame_times(args, meta["duration_us"], requested)
            reader = SourceReader(path, meta, tools, stopped)
            frames, images, citations, seen = [], [], [], set()
            with TemporaryDirectory() as folder:
                for when in times:
                    pts = reader.frame_pts(when)
                    if pts in seen:
                        continue
                    seen.add(pts)
                    jpg = Path(folder) / "frame.jpg"
                    actual = reader.save_frame(pts, jpg, max_width=768)
                    images.append(image_part("image/jpeg", jpg.read_bytes()))
                    frames.append(dict(requested_s=when / US, timestamp_s=actual / US, timestamp_us=actual))
                    if asset:
                        citations.append(citation(asset, actual, actual + 1, "Inspected video frame", "ffmpeg-frame"))
        except MediaStopped as exc:
            raise StaleLease() from exc
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise EditError(422, "FRAME_EXTRACTION_FAILED", "FFmpeg could not decode frames from the selected video.") from exc
        return ToolResult(dict(ok=True, frames=frames, **target), citations, images, related_media=related)

    def timeline(self, db, row, edit_id=None):
        workspace = row.workspace_id
        base = select(projects).where(projects.c.workspace_id == workspace, projects.c.project_id == row.project_id)
        edit_id = edit_id or row.checkpoint.edit_project_id
        if edit_id:
            project = one(db, base.where(projects.c.id == edit_id))
        else:
            # UUIDs are random. The initial revision holds the project's creation timestamp.
            project = db.execute(
                base.join(revisions, (revisions.c.edit_id == projects.c.id) & (revisions.c.number == 0))
                .order_by(revisions.c.snapshot["created_at"].as_string().desc(), projects.c.id.desc()).limit(1)
            ).first()
        if project is None:
            return None, None
        revision = one(db, owned(revisions, workspace, project.head_revision_id)).snapshot
        return project, revision

    def timeline_view(self, db, row, project, revision):
        if project is None:
            return dict(edit_project_id=None, clips=[], total_duration_s=0)
        media_ids = self.bind_timeline(db, row, project, revision)
        by_media = dict(db.execute(select(sources.c.revision, sources.c.id).where(
            sources.c.workspace == row.workspace_id, sources.c.revision.in_(media_ids)
        )).all())
        return dict(
            edit_project_id=project.id, title=project.title, head_revision_id=revision["revision_id"],
            head_revision_no=revision["revision_no"], caption_policy=revision["caption_policy"],
            total_duration_s=revision["total_duration_us"] / US,
            clips=[
                dict(clip_id=c["clip_id"], asset_id=by_media[c["media_revision_id"]],
                     media_revision_id=c["media_revision_id"], start_s=c["source_start_us"] / US,
                     end_s=c["source_end_us"] / US, audio_policy=c["audio_policy"])
                for c in revision["clips"]
            ],
        )

    def bind_timeline(self, db, row, project, revision):
        media_ids = [clip["media_revision_id"] for clip in revision["clips"]]
        self.repo.sources.require_readable(db, row.workspace_id, media_ids)
        row.checkpoint.remember_media(media_ids)
        row.checkpoint.set_timeline(project.id, revision["revision_id"], revision["revision_no"])
        return media_ids

    def get_timeline(self, db, row, args, _call_id):
        view = self.timeline_view(db, row, *self.timeline(db, row, args.edit_project_id))
        clips = view["clips"]
        limit = 20
        view.update(clips=clips[args.offset:args.offset + limit], clip_count=len(clips),
                    next_offset=args.offset + limit if args.offset + limit < len(clips) else None)
        return ToolResult(dict(ok=True, **view))

    def edit_timeline(self, db, row, args, call_id):
        if args.clips is not None and args.operations is not None:
            raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Choose clips or operations for the timeline edit.")
        if args.clips is None and args.operations is None and args.caption_policy is None:
            raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Provide clips or operations, or set only the caption policy.")
        workspace, cp = row.workspace_id, row.checkpoint
        project, revision = self.timeline(db, row, args.edit_project_id)
        if project is None:
            edit = self.repo.edits.create(
                workspace, dict(project_id=row.project_id, title=args.title or "Rough cut", output_profile=DEFAULT_PROFILE,
                                idempotency_key=stable_id(row.id, f"create:{call_id}")), session=db,
            )
            project, revision = self.timeline(db, row, edit["edit_project_id"])
        expected = args.expected_revision if args.expected_revision is not None else cp.timeline_heads.get(
            project.id, project.head
        )
        if expected != project.head:
            raise EditError(409, "REVISION_CONFLICT", "The timeline head changed after it was read.")
        assets = self.assets(row, db=db)
        operations = []
        if args.clips is not None:
            operations.extend(dict(op="remove_clip", clip_id=c["clip_id"]) for c in revision["clips"])
            operations.extend(dict(op="append_clip", clip=self.clip(assets, draft)) for draft in args.clips)
        for op in args.operations or []:
            operation = op.model_dump(exclude_none=True)
            if op.op in ("append_clip", "replace_clip"):
                operation["clip"] = self.clip(assets, op.clip)
            elif op.op == "restore_revision":
                if (op.source_revision_id is None) == (op.revision_no is None):
                    raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Restore by source_revision_id or revision_no.")
                if op.revision_no is not None:
                    previous = one(db, select(revisions).where(
                        revisions.c.workspace_id == workspace, revisions.c.edit_id == project.id,
                        revisions.c.number == op.revision_no,
                    ))
                    operation = dict(op=op.op, source_revision_id=previous.id)
            operations.append(operation)
        if args.caption_policy is not None:
            operations.append(dict(op="set_caption_policy", caption_policy=args.caption_policy))
        result = self.repo.edits.patch(workspace, project.id, dict(
            expected_revision=expected, operations=operations, reason="Agent timeline edit",
            idempotency_key=stable_id(row.id, f"edit:{call_id}"),
        ), session=db)
        next_revision = one(db, owned(revisions, workspace, result["revision_id"])).snapshot
        self.bind_timeline(db, row, project, next_revision)
        change = {key: result[key] for key in ("edit_project_id", "revision_id", "revision_no")}
        return ToolResult(
            dict(ok=True, **change, clip_count=len(next_revision["clips"]),
                 total_duration_s=next_revision["total_duration_us"] / US,
                 clip_id_assignments=result["clip_id_assignments"]),
            notices=[("TIMELINE_UPDATED", change)],
        )

    def clip(self, assets, draft):
        asset = self.asset(assets, draft.asset_id, draft.media_revision_id)
        seconds, micros = (draft.start_s, draft.end_s), (draft.source_start_us, draft.source_end_us)
        if None not in seconds and micros == (None, None):
            start, end = (round(value * US) for value in seconds)
        elif None not in micros and seconds == (None, None):
            start, end = micros
        else:
            raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Supply one complete start and end pair for each clip.")
        if not 0 <= start < end <= asset.probe["duration_us"]:
            raise EditError(422, "INVALID_RANGE", f"Clip must satisfy 0 <= start < end <= {asset.probe['duration_us'] / US:g}s.")
        clip = dict(media_revision_id=asset.revision, source_start_us=start, source_end_us=end,
                    audio_policy=draft.audio_policy, evidence_ids=draft.evidence_ids)
        if draft.crop:
            clip["crop"] = draft.crop.model_dump()
        if draft.caption_track_id:
            clip["caption_track_id"] = draft.caption_track_id
        return clip

    def render_video(self, db, row, args, call_id):
        project, revision = self.timeline(db, row, args.edit_project_id)
        if not revision or not revision["clips"]:
            raise EditError(422, "EMPTY_TIMELINE", "Add clips to the timeline before rendering.")
        if row.checkpoint.timeline_heads.get(project.id, project.head) != project.head:
            raise EditError(409, "REVISION_CONFLICT", "The timeline head changed after it was read.")
        self.bind_timeline(db, row, project, revision)
        job = self.render_worker.repo.enqueue(row.workspace_id, dict(
            idempotency_key=stable_id(row.id, f"render:{call_id}"),
            revision_id=revision["revision_id"], profile=args.profile,
        ), export_format=args.format, session=db)
        return PendingRender(
            tool_call_id=call_id, render_job_id=job["render_job_id"], profile=args.profile,
            edit_project_id=project.id, revision_id=revision["revision_id"],
        )

    def render_result(self, row, pending, job):
        info = pending.model_dump(exclude={"tool_call_id"})
        info.update(status=job["status"], artifact_id=job["artifact_id"], duration_s=None, mime_type=None)
        if job["status"] != "SUCCEEDED":
            error = job["error"] or {}
            return tool_error(error.get("code", "RENDER_CANCELLED"), error.get("message", "The render was cancelled."),
                              "Check the timeline and render again if the user still needs the output.", **info)
        workspace = row.workspace_id
        artifact = self.render_worker.artifact_record(workspace, job["artifact_id"])
        self.render_worker.materialize(artifact)
        validation = self.render_worker.artifact_record(workspace, job["validation_report_id"])
        with self.render_worker.materialize(validation).open(encoding="utf-8") as stream:
            duration = json.load(stream)["duration_us"]
        info.update(duration_s=duration / US, mime_type=self.render_worker.mime_type(artifact))
        notice = dict(kind=pending.profile, artifact_id=artifact.id,
                      edit_project_id=pending.edit_project_id, revision_id=pending.revision_id)
        return ToolResult(dict(ok=True, **info), notices=[("ARTIFACT_READY", notice)])

    @staticmethod
    def ask_user(_db, _row, args, call_id):
        return PendingAsk(tool_call_id=call_id, question=args.question, options=args.options)


def frame_times(args, duration, count):
    if args.timestamps_s is not None:
        if args.start_s is not None or args.end_s is not None or args.count is not None:
            raise EditError(422, "INVALID_TOOL_ARGUMENTS", "Use timestamps_s or start_s/end_s/count for one interval.")
        times = [round(value * US) for value in args.timestamps_s]
        if any(not 0 <= time < duration for time in times):
            raise EditError(422, "INVALID_RANGE", f"Frame timestamps must be before {duration / US:g}s.")
        return sorted(times)
    start = round((args.start_s or 0) * US)
    end = duration if args.end_s is None else round(args.end_s * US)
    if not 0 <= start < end <= duration:
        raise EditError(422, "INVALID_RANGE", f"Frame interval must satisfy 0 <= start < end <= {duration / US:g}s.")
    return [start + (end - start) * (2 * i + 1) // (2 * count) for i in range(count)]
