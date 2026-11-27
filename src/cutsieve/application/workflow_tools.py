"""Read tools available while the coordinator is deciding which edits to propose."""

from cutsieve.application.edit_timeline import EditError, missing
from cutsieve.application.evidence_search import SUBTITLE_PROTOCOL, coverage, subtitle_literal
from cutsieve.application.spec_contracts import tool_catalog
from cutsieve.application.workflow_context import read_reference
from cutsieve.infrastructure.persistence.edit_repository import job_view
from cutsieve.infrastructure.persistence.edit_tables import revisions, jobs


READ_TOOLS = frozenset(tool_catalog().tools)


def job_status(repo, search, run, key):
    cp = run.checkpoint
    if inspection := cp.context.inspections.get(key):
        report = inspection.get("report", {})
        state = inspection["status"]
        active = state in ("QUEUED", "RUNNING")
        cancelled = active and (
            inspection["turn"] != cp.attempt.turn
            or inspection["plan_generation"] != cp.attempt.plan_generation
            or run.state["status"] in ("CANCELLED", "SUPERSEDED")
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
    if key in cp.context.processing_job_ids:
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
    if not key or key not in (cp.attempt.render_job_id, cp.attempt.export_job_id):
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


def read_tool(repo, search, run, name, args, *, references=None):
    checkpoint = run.checkpoint
    scope = checkpoint.spec.scope.model_dump(mode="json")
    workspace = run.workspace_id
    generation = checkpoint.spec.index_generation_id
    if name == "video.search_evidence":
        if args["scope"] != scope:
            raise missing()
        if args.get("image_artifact_id") not in (
            None,
            (checkpoint.spec.search_request or {}).get("image_artifact_id"),
        ):
            raise missing()
        if generation is None:
            raise EditError(409, "INDEX_UNPINNED", "素材还没有可固定的索引")
        return search.search(workspace, args, generation_id=generation)
    if name == "jobs.get":
        return job_status(repo, search, run, args["job_id"])
    if name in ("edit.get_timeline", "edit.validate_timeline"):
        if args["revision_id"] not in (checkpoint.attempt.base_id, checkpoint.attempt.revision_id):
            raise missing()
        edit_id = checkpoint.attempt.edit_id
        if not edit_id or args.get("edit_project_id", edit_id) != edit_id:
            raise missing()
        revision = repo.edits.get(
            revisions, workspace, args["revision_id"], lambda r: r.snapshot, edit_id=edit_id
        )
        if name == "edit.get_timeline":
            return revision
        return dict(valid=True, errors=[], warnings=[] if revision["clips"] else ["尚无片段"])
    if name not in READ_TOOLS:
        raise EditError(403, "TOOL_DENIED", "此阶段没有执行该工具的权限")
    if name == "video.get_context_window":
        if max(args["before_us"], args["after_us"]) > 15_000_000:
            raise EditError(422, "CONTEXT_WINDOW", "证据窗口向前、向后各最多 15 秒")
        evidence = read_reference(repo, run, args["evidence_id"], references=references)
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
    options = {"generation_id": generation} if generation else {}
    if name == "video.check_coverage":
        if args["query_run_id"] != run.id:
            raise missing()
        checked = {}
        if (
            args["protocol_id"] == SUBTITLE_PROTOCOL
            and subtitle_literal(checkpoint.spec.search_request or {})
        ):
            frozen, _ = search.catalog.search_scope(workspace, scope["project_id"], [mid], **options)
            checked = search.catalog.literal_coverage(workspace, scope["project_id"], frozen, [mid])
        summary = coverage([dict(media_revision_id=mid, range=span)], run_id=run.id, checked=checked)
        return dict(
            status=summary["status"],
            unverified_ranges=[gap["range"] for gap in summary["unverified_ranges"]],
        )
    options["span"] = span
    _, rows = search.catalog.search_scope(workspace, scope["project_id"], [mid], **options)
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
        result = dict(evidence_ids=ids, range=span)
        offset = args.get("offset", 0)
        details = [evidence] + [r for r in matched if r["evidence_id"] != evidence["evidence_id"]]
        result.update(
            evidence_ids=[item["evidence_id"] for item in details[offset : offset + 12]],
            excerpts=[
                dict(
                    evidence_id=item["evidence_id"],
                    range=item["range"],
                    support=item["support"],
                    text=item["claim"][:400],
                    artifact_ids=item["artifact_ids"][:4],
                )
                for item in details[offset : offset + 12]
            ],
            next_offset=offset + 12 if offset + 12 < len(details) else None,
        )
        return result
    return dict(
        evidence_ids=ids,
        artifact_ids=list(dict.fromkeys(i for r in matched for i in r["artifact_ids"]))[:100],
        range=span,
    )
