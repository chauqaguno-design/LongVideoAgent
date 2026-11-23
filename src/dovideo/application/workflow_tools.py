"""Read tools available while the coordinator is deciding which edits to propose."""

from dovideo.application.edit_timeline import EditError, missing
from dovideo.application.evidence_search import query_literal, split_coverage
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.persistence.edit_repository import job_view
from dovideo.infrastructure.persistence.edit_tables import revisions, jobs


READ_TOOLS = {
    "video.search_evidence",
    "video.get_segment",
    "video.get_context_window",
    "video.check_coverage",
    "audio.inspect_transcript",
    "edit.get_timeline",
    "edit.validate_timeline",
    "jobs.get",
}


def job_status(repo, search, run, key):
    cp = run.checkpoint
    if inspection := cp.get("inspections", {}).get(key):
        report = inspection.get("report", {})
        state = inspection["status"]
        active = state in ("QUEUED", "RUNNING")
        cancelled = active and (
            inspection["turn"] != cp["turn"] or run.state["status"] in ("CANCELLED", "SUPERSEDED")
        )
        failed = active and run.state["status"] == "FAILED"
        return dict(
            job_id=key,
            kind="INSPECTION",
            status="CANCELLED" if cancelled else "FAILED" if failed else state,
            terminal=cancelled or failed or not active,
            cancel_requested_at=run.state["cancel_requested_at"] if cancelled else None,
            result_artifact_ids=[report["sequence_id"]] if report.get("sequence_id") else [],
            error=run.state["error"] if failed else inspection.get("error"),
        )
    if key in cp.get("processing_job_ids", []):
        job = search.catalog.get(run.workspace_id, key)
        return dict(
            job_id=key,
            kind="PROCESSING",
            status=job["status"],
            terminal=job["status"] in ("PARTIAL", "SUCCEEDED", "FAILED", "CANCELLED"),
            cancel_requested_at=job["cancel_requested_at"],
            error=job["error"],
            result_artifact_ids=[],
        )
    if not key or key not in (cp.get("render_job_id"), cp.get("export_job_id")):
        raise missing()
    job = repo.edits.get(jobs, run.workspace_id, key, job_view)
    return dict(
        job_id=key,
        kind="RENDER",
        status=job["status"],
        terminal=job["status"] in ("SUCCEEDED", "FAILED", "CANCELLED"),
        cancel_requested_at=job["cancel_requested_at"],
        error=job["error"],
        result_artifact_ids=[job["artifact_id"]] if job["artifact_id"] else [],
    )


def read_tool(repo, search, run, name, args):
    checkpoint = run.checkpoint
    scope = checkpoint["scope"]
    workspace = run.workspace_id
    if name == "video.search_evidence":
        if args["scope"] != scope:
            raise missing()
        if args.get("image_artifact_id") not in (
            None,
            checkpoint.get("search_request", {}).get("image_artifact_id"),
        ):
            raise missing()
        return search.search(workspace, args)
    if name == "jobs.get":
        return job_status(repo, search, run, args["job_id"])
    if name in ("edit.get_timeline", "edit.validate_timeline"):
        if args["revision_id"] not in (checkpoint.get("base_id"), checkpoint.get("revision_id")):
            raise missing()
        edit_id = checkpoint.get("edit_id")
        if not edit_id or args.get("edit_project_id", edit_id) != edit_id:
            raise missing()
        revision = repo.edits.get(
            revisions, workspace, args["revision_id"], lambda r: r.snapshot, edit_id=edit_id
        )
        if name == "edit.get_timeline":
            return revision
        validate_contract("TimelineRevision", revision)
        return dict(valid=True, errors=[], warnings=[] if revision["clips"] else ["尚无片段"])
    if name not in READ_TOOLS:
        raise EditError(403, "TOOL_DENIED", "此阶段没有执行该工具的权限")
    if name == "video.get_context_window":
        evidence = repo.edits.resolve_evidence(workspace, args["evidence_id"])
        mid = evidence["media_revision_id"]
    else:
        mid = args["media_revision_id"]
    if mid not in scope["media_revision_ids"]:
        raise missing()
    duration = repo.sources.media(workspace, mid)["duration_us"]
    if name == "video.get_context_window":
        span = dict(
            start_us=max(0, evidence["range"]["start_us"] - args["before_us"]),
            end_us=min(duration, evidence["range"]["end_us"] + args["after_us"]),
        )
    else:
        span = args["range"]
    if not 0 <= span["start_us"] < span["end_us"] <= duration:
        raise EditError(422, "RANGE", "范围超出原片")
    if name == "video.check_coverage":
        if args["query_run_id"] != run.id:
            raise missing()
        request = checkpoint.get("search_request", {})
        text = request.get("text") or ""
        checked = {}
        if (
            args["protocol_id"] == "embedded-subtitle-literal-v1"
            and not request.get("image_artifact_id")
            and query_literal(text)
            and ("字幕" in text or "subtitle" in text.lower())
        ):
            generation, _ = search.catalog.search_scope(workspace, scope["project_id"], [mid])
            checked = search.catalog.literal_coverage(workspace, scope["project_id"], generation, [mid])
        _, gaps = split_coverage([dict(media_revision_id=mid, range=span)], checked)
        return dict(
            status="PARTIAL" if gaps else "PROTOCOL_COMPLETE",
            unverified_ranges=[gap["range"] for gap in gaps],
        )
    _, rows = search.catalog.search_scope(workspace, scope["project_id"], [mid])
    matched = [
        r
        for r in rows
        if r["media_revision_id"] == mid
        and r["range"]["start_us"] < span["end_us"]
        and r["range"]["end_us"] > span["start_us"]
    ]
    if name == "audio.inspect_transcript":
        spoken = [r for r in matched if r["source"] == "faster-whisper"]
        return dict(
            observation_ids=list(dict.fromkeys(i for r in spoken for i in r["observation_ids"]))[:100],
            quality_flags=[] if spoken else ["NO_TRANSCRIPT_IN_RANGE"],
            conflict_evidence_ids=[r["evidence_id"] for r in matched if r["support"] == "CONFLICTING"][:100],
        )
    ids = [r["evidence_id"] for r in matched[:100]]
    if name == "video.get_context_window":
        return dict(evidence_ids=ids, range=span)
    return dict(
        evidence_ids=ids,
        artifact_ids=list(dict.fromkeys(i for r in matched for i in r["artifact_ids"]))[:100],
        range=span,
    )
