"""A chat turn points to its parent run and to exactly one immutable source video."""

from dovideo.application.edit_instructions import clarified_instruction
from dovideo.application.render_formats import EXPORT_FORMATS
from dovideo.application.workflow_routing import (
    candidate_reference,
    flash_route,
    references_edit,
    resolve_route,
)
from dovideo.application.workflow_state import NeedsInput
from dovideo.infrastructure.persistence.edit_tables import revisions
from dovideo.infrastructure.persistence.workflow_budget import run_model


def candidate_cards(repo, workspace, claims, media_id, session=None):
    """Keep screen numbering stable, including separate occurrences of identical words."""
    candidates, seen = [], set()
    for claim in claims:
        for key in claim["evidence_ids"]:
            evidence = repo.edits.resolve_evidence(workspace, key, session=session)
            if evidence["media_revision_id"] != media_id:
                continue
            span = evidence["range"]
            location = (span["start_us"], span["end_us"])
            if location not in seen:
                seen.add(location)
                candidates.append(
                    dict(
                        media_revision_id=media_id,
                        range=span,
                        evidence_ids=[key],
                        text=claim["text"],
                        support=claim["support"],
                    )
                )
            if len(candidates) == 10:
                return candidates
    return candidates


def conversation(repo, workspace, run_id):
    """Read only the explicit reply chain. Old, single-video results can start a new conversation."""
    turns = []
    with repo.sessions() as db:
        for _ in range(20):
            row = repo.readable(db, workspace, run_id)
            cp = row.checkpoint
            media = cp["scope"]["media_revision_ids"]
            media_id = media[0] if len(media) == 1 else None
            output = {}
            if row.state["result_artifact_id"]:
                output = repo.document(workspace, row.state["result_artifact_id"], session=db).body
            parent = cp.get("conversation", {}).get("parent_run_id")
            candidates = cp.get("conversation_candidates") if output else None
            if candidates is None:
                candidates = candidate_cards(repo, workspace, output.get("claims", []), media_id, db)
            turns.append(
                dict(
                    run_id=row.id,
                    parent_run_id=parent,
                    media_revision_id=media_id,
                    message=cp["message"],
                    status=row.status,
                    route=cp.get("route_decision"),
                    answer=output.get("answer"),
                    edit_project_id=output.get("edit_project_id"),
                    revision_id=output.get("revision_id"),
                    clarifications=cp.get("clarifications", []),
                    candidates=candidates,
                )
            )
            if not parent:
                break
            run_id = parent
    return {"turns": list(reversed(turns))}


def route_turn(repo, planner, row):
    cp = row.checkpoint
    text = cp["message"]
    reply = cp.get("clarification") or ""
    parent = cp["conversation"].get("parent_run_id")
    history = conversation(repo, row.workspace_id, parent)["turns"] if parent else []
    last_edit = next((t for t in reversed(history) if t["revision_id"] and t["edit_project_id"]), None)
    # A short answer to a routing question can supply just the missing intent or ordinal.
    original = resolve_route(text, has_edit=bool(last_edit))
    decision = original
    if reply:
        clarified = resolve_route(reply, has_edit=bool(last_edit))
        if clarified.intent or not original.intent:
            decision = clarified
    reference = candidate_reference(reply) if reply else None
    instruction = clarified_instruction(text, reply)
    if reference is None:
        reference = candidate_reference(instruction)

    if (
        decision.intent is None
        and planner.env.get("DOVIDEO_WORKFLOW_LIVE_MODEL") == "1"
        and row.state["budget_usage"]["limits"]["max_cost_microunits"] > 0
    ):

        def classify(dialogs):
            identity = planner.identity()
            return run_model(
                repo,
                row,
                f"route-{cp['turn']}",
                dialogs,
                [],
                planner.env,
                lambda: planner.request(dialogs, identity, [], repo.remaining(row)),
                model=identity["model"],
            )

        decision = flash_route(
            text + ("\n用户补充：" + reply if reply else ""), has_edit=bool(last_edit), call=classify
        )

    route = decision.as_dict()
    changes = {"route_decision": route}
    question = decision.question
    options = []
    if decision.intent and reference is not None:
        choices = next((t["candidates"] for t in reversed(history) if t["candidates"]), [])
        number = 1 if reference == 0 and len(choices) == 1 else reference
        if 1 <= number <= len(choices):
            changes["selected_candidate"] = choices[number - 1]
        else:
            question = (
                f"上一轮列出了 {len(choices)} 个候选。你指的是哪一段？"
                if choices
                else "这段对话里还没有候选。请说明要查找的画面、台词或原片时间范围。"
            )
            options = [f"第 {n} 段" for n in range(1, len(choices) + 1)]
    reuse_edit = references_edit(instruction) or (
        decision.intent == "EDIT" and "EDIT" not in decision.plan
    )
    if decision.intent == "EDIT" and reference is None and reuse_edit:
        if not last_edit:
            question = "这段对话还没有剪辑版本。请先指定要剪的台词、候选或原片时间范围。"
        else:
            rev = repo.edits.get(
                revisions,
                row.workspace_id,
                last_edit["revision_id"],
                lambda r: r.snapshot,
                edit_id=last_edit["edit_project_id"],
            )
            changes.update(
                edit_id=last_edit["edit_project_id"], base=rev["revision_no"], base_id=rev["revision_id"]
            )
    if question:
        route["question"] = question
        return dict(
            checkpoint=changes,
            state=dict(status="AWAITING_INPUT", awaiting_input=NeedsInput(question, options).input),
            event="NEEDS_INPUT",
        )

    changes["intent"] = decision.intent
    if decision.plan == ("ANSWER",):
        formats = "、".join(name.upper() for name in EXPORT_FORMATS)
        return dict(
            checkpoint=changes,
            state=dict(phase="DONE", status="SUCCEEDED"),
            event="COMPLETED",
            docs={"result": {"run_id": row.id, "answer": f"成片可以导出为 {formats}。默认使用 MP4。可在时间线选择格式，或在对话里说“把上一版导出为 WebM”。"}},
        )
    # Multi-role execution uses the existing task board. Plain lookups need no team.
    changes["team"] = cp["conversation"]["allow_team"] and (
        "INSPECT" in decision.plan or "EDIT" in decision.plan
    )
    return dict(
        checkpoint=changes,
        state=dict(phase="PLAN"),
        event="PROGRESS",
        payload={"summary": decision.reason, "route": decision.intent, "steps": list(decision.plan)},
    )
