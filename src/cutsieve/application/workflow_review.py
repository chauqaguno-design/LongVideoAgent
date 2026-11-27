"""Read render measurements and choose at most one justified clip replacement per review."""

import json

from .edit_timeline import EditError, apply_patch
from .spec_contracts import validate_contract
from .workflow_checkpoint import ClipRepair
from .workflow_identity import digest_json, generation_key
from .workflow_shots import disjoint, replace_operation, validate_refinement
from .workflow_state import NeedsInput, stable_id
from cutsieve.infrastructure.edit_media import RENDERER_VERSION
from cutsieve.infrastructure.media.source_artifacts import digest_file


def render_document(worker, row, key, kind):
    record = worker.artifact_record(row.workspace_id, key)
    if record.kind != kind or record.revision_id != row.checkpoint.attempt.revision_id:
        raise EditError(409, "OUTPUT_REVISION_CHANGED", "复核产物与当前时间线版本不符")
    return record, worker.materialize(record)


def checked_render(worker, row, job):
    kind = job["profile"]
    output, path = render_document(worker, row, job["artifact_id"], kind)
    validation_id = job.get("validation_report_id")
    if not validation_id:
        # Without a published validation report, the output digest needs an independent check.
        if digest_file(path) != output.digest:
            raise EditError(422, "OUTPUT_DIGEST_CHANGED", "复核文件与登记摘要不符")
        raise EditError(422, "RENDER_VALIDATION", "成片缺少已发布的校验报告")
    _, validated = render_document(worker, row, validation_id, "VALIDATION_REPORT")
    _, compiled = render_document(worker, row, job["compiled_manifest_artifact_id"], "COMPILED_TIMELINE")
    qa = json.loads(validated.read_text("utf8"))
    manifest = json.loads(compiled.read_text("utf8"))
    structure = qa["media"]["structure"]
    if (
        qa["revision_id"] != output.revision_id
        or manifest["revision_id"] != output.revision_id
        or structure["output_sha256"] != output.digest
        or structure["status"] != "PASSED"
        or not qa["full_decode"]
    ):
        raise EditError(422, "RENDER_VALIDATION", "成片的结构校验和摘要无法对应")
    return output, manifest


def review_report(row, kind, output=None, manifest=None, semantic=None, failed=False):
    cp = row.checkpoint
    check = "FAILED" if failed else "PASSED" if output else "NOT_CHECKED"
    span = None if manifest is None else dict(start_us=0, end_us=manifest["total_duration_us"])
    semantic = semantic or {}
    times = semantic.get("checked_times_us", [])
    status = {"SUPPORTED": "PASSED", "CONFLICTING": "FAILED", "UNCERTAIN": "UNCERTAIN"}.get(
        semantic.get("support"),
        "NOT_CHECKED",
    )
    return dict(
        report_id=stable_id(row.id, generation_key(cp, f"{kind.lower()}_review:{cp.attempt.turn}")),
        generation=cp.attempt.plan_generation,
        timeline_revision_id=cp.attempt.revision_id,
        output_kind=kind,
        output_artifact_id=output.id if output else None,
        output_sha256=output.digest if output else None,
        coverage=dict(
            structural_range=span,
            sampled_times_us=semantic.get("sampled_times_us", []),
            checked_times_us=times,
            sequence_id=semantic.get("sequence_id"),
        ),
        structural_checks=[dict(code="FFMPEG_VALIDATION", status=check)],
        semantic_checks=[dict(code="SAMPLED_CONTENT", status=status)],
        issues=[],
    )


def review_issue(report, semantic, manifest):
    status = report["semantic_checks"][0]["status"]
    if status not in ("FAILED", "UNCERTAIN"):
        return None
    times = set(report["coverage"]["checked_times_us"])
    ids = list(dict.fromkeys(
        clip["clip_id"]
        for clip in manifest["clips"]
        if any(clip["output_start_us"] <= t < clip["output_end_us"] for t in times)
    ))
    return dict(
        code="SAMPLED_CLIP_CONFLICT" if status == "FAILED" and ids else "CONTENT_UNCONFIRMED",
        severity="ERROR" if status == "FAILED" else "WARNING",
        constraint_refs=["must_include" if manifest and ids else "goal"],
        evidence_refs=[semantic["sequence_id"]] if semantic.get("sequence_id") else [],
        affected_clip_ids=ids,
        repairability="MANUAL",
        explanation=semantic["reason"],
    )


def repair_for(row, issue, revision):
    cp = row.checkpoint
    if (
        issue["code"] != "SAMPLED_CLIP_CONFLICT"
        or len(issue["affected_clip_ids"]) != 1
        or cp.repair.repair_count >= cp.repair.repair_limit
        or not cp.attempt.shot_plan
        or cp.spec.brief.must_exclude
    ):
        return None
    usage = row.state["budget_usage"]
    limits = usage["limits"]
    spent = sum(
        usage[k] or 0 for k in ("committed_microunits", "reserved_microunits", "uncertain_microunits")
    )
    if (
        usage["model_calls"] >= limits["max_model_calls"]
        or usage["images"] + 2 > limits["max_images"]
        or usage["tool_calls"] + 2 > limits["max_tool_calls"]
        or spent >= limits["max_cost_microunits"]
    ):
        return None
    (target,) = issue["affected_clip_ids"]
    original = next(c for c in revision["clips"] if c["clip_id"] == target)
    if not any(
        shot["target_clip_id"] in (None, target) and shot["evidence_ids"] == original["evidence_ids"]
        for shot in cp.attempt.shot_plan["shots"]
    ):
        return None
    rejected = set(cp.repair.rejected_candidates)
    rejected.update(
        shot["candidate_id"]
        for shot in cp.attempt.shot_plan["shots"]
        if shot["evidence_ids"] == original["evidence_ids"]
    )
    for candidate in cp.attempt.candidate_snapshot:
        mid, span, refs = candidate["media_revision_id"], candidate["range"], candidate["evidence_ids"]
        if (
            candidate["candidate_id"] in rejected
            or candidate["support"] != "SUPPORTED"
            or not refs
            or any(not disjoint(clip, mid, span) for clip in revision["clips"])
        ):
            continue
        source = dict(
            media_revision_id=mid,
            source_start_us=span["start_us"],
            source_end_us=span["end_us"],
            evidence_ids=refs,
            audio_policy=original["audio_policy"],
        )
        if mid != original["media_revision_id"]:
            continue
        operation = replace_operation(original, source, preserve_audio=True)
        signature = digest_json(
            [
                issue["code"],
                operation,
                RENDERER_VERSION,
                revision["output_profile"],
                revision["caption_policy"],
                cp.attempt.export_format,
            ]
        )
        if signature in cp.repair.signatures:
            continue
        draft, _ = apply_patch(revision, [operation], None, lambda c: None)
        try:
            validate_refinement(revision, draft, cp.spec.brief.model_dump(mode="json"), [target], grounded=True)
        except NeedsInput:
            continue
        issue["repairability"] = "AUTO"
        return ClipRepair(
            signature=signature,
            rejected_candidates=sorted(rejected),
            attempt=cp.attempt.for_repair(
                base=revision["revision_no"], base_id=revision["revision_id"], original=original,
                interval=(mid, span, refs),
                preserved=[clip["clip_id"] for clip in revision["clips"] if clip["clip_id"] != target],
            ),
        )
    return None


def checked_report(report):
    validate_contract("ReviewReport", report)
    return report
