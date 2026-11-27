"""Propose a timeline patch, commit it or drive its revision-bound render job."""

from sqlalchemy import select

from ..edit_instructions import append_ranges
from ..workflow_checkpoint import PlanAttempt
from ..workflow_identity import generation_key
from ..workflow_shots import grounded_plan, local_brief, replace_operation, validate_refinement
from ..workflow_skills import use_skill
from ..workflow_state import NeedsInput
from ...infrastructure.persistence.edit_repository import job_view
from ...infrastructure.persistence.edit_tables import jobs, revisions
from . import StageDeps, base_revision, call_tool, source_proofs, step
from .review import render_failure


def run_edit(deps: StageDeps, row):
    match row.state["phase"]:
        case "PROPOSE":
            return _propose(deps, row)
        case "COMMIT":
            return _commit(deps, row)
        case "RENDER":
            return _render(deps, row)
        case phase:
            raise KeyError(phase)


def _propose(deps, row):
    cp = row.checkpoint
    skill_name = "timeline-refinement" if cp.attempt.target_clip_ids else "grounded-editing"
    use_skill(deps.repo, row, skill_name, {"edit.get_timeline", "edit.validate_timeline"})
    plan = cp.attempt.plan
    if plan["command"] == "render_current":
        if not cp.attempt.base_id:
            raise NeedsInput("请先选定要导出的剪辑版本。")
        operations = []
    elif plan["command"] in ("quote", "semantic", "ranges", "candidate", "adjust_clip"):
        targets = cp.attempt.target_clip_ids
        base = base_revision(deps, row)
        brief = cp.spec.brief.model_dump(mode="json")
        if targets or plan["command"] in ("quote", "semantic"):
            brief = local_brief(brief, base, targets)
        operations = append_ranges(brief, cp.attempt.intervals)
        if original := cp.attempt.original_clip:
            operations = [
                replace_operation(
                    original,
                    operations[0]["clip"],
                    preserve_audio=bool(plan.get("preserve_audio", "audio" not in plan)),
                )
            ]
    else:
        if not cp.attempt.edit_id:
            raise NeedsInput("请在已有剪辑方案中选择要修改的时间线。")
        snap = deps.repo.edits.get(
            revisions, row.workspace_id, cp.attempt.base_id, lambda r: r.snapshot, edit_id=cp.attempt.edit_id
        )
        ids = [c["clip_id"] for c in snap["clips"]]
        if plan["command"] == "restore":
            with deps.repo.sessions() as db:
                key = db.execute(
                    select(revisions.c.id).where(
                        revisions.c.edit_id == cp.attempt.edit_id,
                        revisions.c.workspace_id == row.workspace_id,
                        revisions.c.number == plan["number"],
                    )
                ).scalar()
            if not key:
                raise NeedsInput("这个剪辑方案没有指定的版本，请核对版本号。")
            target = deps.repo.edits.get(revisions, row.workspace_id, key, lambda r: r.snapshot)
            if any(
                c["media_revision_id"] not in cp.spec.scope.media_revision_ids for c in target["clips"]
            ):
                raise NeedsInput("要恢复的版本使用了本次范围之外的素材，请选定完整原片后重新提交。")
            operations = [dict(op="restore_revision", source_revision_id=key)]
        elif not ids:
            raise NeedsInput("这个版本中还没有片段。")
        elif plan["command"] == "remove_last":
            operations = [dict(op="remove_clip", clip_id=ids[-1])]
        else:
            operations = [dict(op="reorder_clips", clip_ids=list(reversed(ids)))]
    proposal = dict(base_revision=cp.attempt.base, operations=operations, brief_id=cp.attempt.brief_id)
    attempt = PlanAttempt(proposal=proposal)
    docs = {"proposal": proposal}
    if plan["command"] == "semantic":
        shots = grounded_plan(row, operations)
        for shot in shots["shots"]:
            _, proofs = source_proofs(
                deps.repo, row, shot["media_revision_id"], shot["range"], shot["evidence_ids"]
            )
            for proof in proofs.values():
                span, required = shot["range"], proof["range"]
                if not (
                    proof["support"] == "SUPPORTED"
                    and proof["verification_protocol"] == "ordered-pts-three-frame-v1"
                    and proof["index_generation_id"] == shots["index_generation_id"]
                    and span["start_us"] <= required["start_us"] < required["end_us"] <= span["end_us"]
                ):
                    raise NeedsInput("镜头区间没有完整包含本轮视频核查证据。")
        attempt.shot_plan = docs["shot_plan"] = shots
    return step("COMMIT", attempt=attempt, docs=docs)


def _commit(deps, row):
    cp = row.checkpoint
    if cp.attempt.plan["command"] == "render_current":
        return step("RENDER", attempt=PlanAttempt(revision_id=cp.attempt.base_id))
    scope = row.workspace_id
    fence = lambda db: deps.repo.writer_guard(db, row, "COMMIT")
    edit_id = cp.attempt.edit_id
    if not edit_id:
        body = dict(
            project_id=row.project_id,
            title=cp.spec.message[:120],
            output_profile=cp.spec.brief.output_profile,
            idempotency_key=f"{row.id}-project",
        )
        edit = call_tool(
            deps, row, "create", "edit.create", body, lambda: deps.repo.edits.create(scope, body, guard=fence)
        )
        edit_id = edit["edit_project_id"]
    proposal = cp.attempt.proposal
    body = dict(
        operations=proposal["operations"],
        expected_revision=proposal["base_revision"],
        reason=cp.spec.message[:1500],
        idempotency_key=generation_key(cp, f"{row.id}-{cp.attempt.turn}-patch"),
    )
    check = None
    if cp.attempt.target_clip_ids or cp.attempt.shot_plan:
        check = lambda parent, draft: validate_refinement(
            parent,
            draft,
            cp.spec.brief.model_dump(mode="json"),
            cp.attempt.target_clip_ids,
            grounded=bool(cp.attempt.shot_plan),
        )
    receipt = call_tool(
        deps, row,
        "patch",
        "edit.apply_patch",
        body,
        lambda: deps.repo.edits.patch(
            scope, edit_id, body, guard=fence, brief_id=cp.attempt.brief_id, validate_result=check
        ),
    )
    attempt = PlanAttempt(edit_id=edit_id, revision_id=receipt["revision_id"])
    if cp.attempt.target_clip_ids:
        snapshot = deps.repo.edits.get(
            revisions, scope, receipt["revision_id"], lambda r: r.snapshot, edit_id=edit_id
        )
        old = cp.attempt.original_clip
        new = next(c for c in snapshot["clips"] if c["clip_id"] == old["clip_id"])
        attempt.timeline_change = dict(
            clip_id=old["clip_id"],
            base_revision_id=cp.attempt.base_id,
            revision_no=snapshot["revision_no"],
            position=next(
                i for i, c in enumerate(snapshot["clips"], 1) if c["clip_id"] == old["clip_id"]
            ),
            preserved_count=len(cp.attempt.preserved_clip_ids),
            before=dict(
                media_revision_id=old["media_revision_id"],
                range=dict(start_us=old["source_start_us"], end_us=old["source_end_us"]),
            ),
            after=dict(
                media_revision_id=new["media_revision_id"],
                range=dict(start_us=new["source_start_us"], end_us=new["source_end_us"]),
            ),
        )
    return step("RENDER", attempt=attempt)


def _render(deps, row):
    cp = row.checkpoint
    mid = cp.attempt.revision_id
    revision = deps.repo.edits.get(revisions, row.workspace_id, mid, lambda r: r.snapshot)
    if not revision["clips"]:
        if cp.attempt.export_requested:
            raise NeedsInput("时间线当前没有片段，请添加片段后导出。")
        return step("REVIEW")
    exporting = bool(
        cp.attempt.export_requested
        and cp.attempt.preview_artifact_id
        and cp.attempt.preview_accepted
    )
    profile = "EXPORT" if exporting else "PREVIEW"
    if not row.state["pending_job_ids"]:
        body = dict(
            revision_id=mid,
            profile=profile,
            idempotency_key=generation_key(cp, f"{row.id}-{cp.attempt.turn}-{profile.lower()}"),
        )
        tool_args = body
        if exporting:
            tool_args = dict(body, export_format=cp.attempt.export_format)
        job = call_tool(
            deps, row,
            profile.lower(),
            "render.submit",
            tool_args,
            lambda: deps.repo.edits.enqueue(
                row.workspace_id,
                body,
                export_format=cp.attempt.export_format if exporting else "mp4",
                guard=lambda db: deps.repo.writer_guard(db, row, "RENDER"),
            ),
        )
        return step(
            "RENDER",
            status="WAITING_JOB",
            pending_job_ids=[job["render_job_id"]],
            attempt=(PlanAttempt(export_job_id=job["render_job_id"]) if exporting
                     else PlanAttempt(render_job_id=job["render_job_id"])),
        )
    job_id = cp.attempt.export_job_id if exporting else cp.attempt.render_job_id
    job = deps.repo.edits.get(jobs, row.workspace_id, job_id, job_view)
    if job["status"] in ("QUEUED", "RUNNING", "VALIDATING"):
        return {"sleep": True}
    if job["status"] != "SUCCEEDED":
        return render_failure(
            row,
            profile,
            "RENDER_FAILED",
            "当前渲染作业未能通过成片校验",
            explanation="本次渲染作业没有生成通过 FFmpeg 校验的文件。",
        )
    trace = dict(
        activity="render.finished",
        profile=profile,
        revision_id=mid,
        render_job_id=job["render_job_id"],
        artifact_id=job["artifact_id"],
        validation_report_id=job["validation_report_id"],
        renderer_version=job["renderer_version"],
    )
    if exporting:
        attempt = PlanAttempt(export_artifact_id=job["artifact_id"], export_qa_id=job["validation_report_id"])
        event = "PROGRESS"
    else:
        attempt = PlanAttempt(preview_artifact_id=job["artifact_id"], qa_id=job["validation_report_id"])
        event = "PREVIEW_READY"
    return step("REVIEW", attempt=attempt, pending_job_ids=[], event=event, payload=trace)
