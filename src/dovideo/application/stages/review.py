"""Review the answer or rendered output, then complete or schedule the bounded repair."""

from ..edit_timeline import EditError
from ..workflow_checkpoint import PlanAttempt
from ..workflow_inspections import inspection
from ..workflow_review import checked_render, checked_report, repair_for, review_issue, review_report
from ..workflow_skills import use_skill
from ..workflow_state import NeedsInput, public_error
from ...infrastructure.persistence.edit_repository import job_view
from ...infrastructure.persistence.edit_tables import jobs, revisions
from ...infrastructure.persistence.workflow_budget import review_budget_available
from . import StageDeps, step


def run_review(deps: StageDeps, row):
    cp = row.checkpoint
    if cp.attempt.intent != "EDIT":
        return step("DONE", status="SUCCEEDED", docs={"result": cp.attempt.answer}, event="COMPLETED")
    use_skill(deps.repo, row, "output-review", set(), purpose="vision")
    exporting = bool(cp.attempt.export_artifact_id)
    kind = "EXPORT" if exporting else "PREVIEW"
    job_id = cp.attempt.export_job_id if exporting else cp.attempt.render_job_id
    semantic = None
    output = manifest = None
    if job_id:
        job = deps.repo.edits.get(jobs, row.workspace_id, job_id, job_view)
        try:
            output, manifest = checked_render(deps.render_worker, row, job)
        except EditError as exc:
            if exc.code not in ("OUTPUT_REVISION_CHANGED", "OUTPUT_DIGEST_CHANGED", "RENDER_VALIDATION"):
                raise
            return render_failure(row, kind, exc.code, str(exc))
        if not exporting and cp.attempt.plan["command"] == "semantic":
            semantic = inspection(
                row,
                "final-output",
                None,
                dict(start_us=0, end_us=manifest["total_duration_us"]),
                cp.spec.brief.model_dump(mode="json"),
                preview=output.id,
            )
    report = review_report(row, kind, output, manifest, semantic)
    label = f"{kind.lower()}_review"
    if semantic and (issue := review_issue(report, semantic, manifest)):
        revision = deps.repo.edits.get(revisions, row.workspace_id, cp.attempt.revision_id, lambda r: r.snapshot)
        repair = (
            repair_for(row, issue, revision)
            if review_budget_available(deps.repo, row, deps.planner.env)
            else None
        )
        report["issues"].append(issue)
        if repair:
            outcome = step(
                "PROPOSE",
                docs={label: checked_report(report)},
                pending_job_ids=[],
                payload=dict(
                    activity="review.repair_scheduled",
                    report_id=report["report_id"],
                    affected_clip_ids=issue["affected_clip_ids"],
                ),
            )
            outcome["repair"] = repair
            return outcome
    docs = {label: checked_report(report)}
    if not exporting and cp.attempt.export_requested and not report["issues"]:
        return step("RENDER", attempt=PlanAttempt(preview_accepted=True), docs=docs, pending_job_ids=[])
    result = dict(
        edit_project_id=cp.attempt.edit_id,
        revision_id=cp.attempt.revision_id,
        brief=cp.spec.brief.model_dump(mode="json"),
        render_job_id=cp.attempt.render_job_id,
        preview_artifact_id=cp.attempt.preview_artifact_id,
        validation_report_id=cp.attempt.qa_id,
        checks=dict(media=report["structural_checks"][0]["status"], semantic=semantic or "NOT_CHECKED"),
        review_report=report,
        repair_count=cp.repair.repair_count,
        shot_plan=cp.attempt.shot_plan,
        timeline_change=cp.attempt.timeline_change,
        lineage=cp.repair.lineage,
        gaps=[],
    )
    if semantic:
        result["gaps"].append("语义审核覆盖本报告列出的抽帧时刻。")
    if exporting:
        result.update(
            export_artifact_id=cp.attempt.export_artifact_id,
            export_format=cp.attempt.export_format,
            export_validation_report_id=cp.attempt.export_qa_id,
            export_job_id=cp.attempt.export_job_id,
            preview_review_id=cp.attempt.preview_review_id,
        )
        if cp.attempt.preview_review_id:
            result["preview_review"] = deps.repo.document(row.workspace_id, cp.attempt.preview_review_id).body
    if cp.attempt.plan["command"] == "candidate" and cp.attempt.selected_candidate.get("support") != "SUPPORTED":
        result["gaps"].append("本次按你选中的时间裁剪，候选中的人物或动作仍待核实。")
    docs["result"] = result
    if report["issues"]:
        waiting = NeedsInput("请回看预览并补充要求。当前证据或剩余额度不足以继续自动修复。")
        return step(
            "REVIEW",
            status="AWAITING_INPUT",
            awaiting_input=waiting.input,
            event="NEEDS_INPUT",
            docs=docs,
        )
    return step("DONE", status="SUCCEEDED", docs=docs, event="COMPLETED")


def render_failure(row, kind, code, message, *, explanation=None):
    report = review_report(row, kind, failed=True)
    report["issues"].append(
        dict(
            code=code,
            severity="ERROR",
            constraint_refs=["output_profile"],
            evidence_refs=[],
            affected_clip_ids=[],
            repairability="MANUAL",
            explanation=explanation or message,
        )
    )
    return step(
        row.state["phase"],
        status="FAILED",
        event="FAILED",
        pending_job_ids=[],
        docs={f"{kind.lower()}_review": checked_report(report)},
        error=public_error(code, message),
    )
