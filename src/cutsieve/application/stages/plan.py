"""Build the edit brief and, when needed, resume one model planning round."""

import json
import re

from fastapi import HTTPException

from ..edit_instructions import (
    AUDIO, PROFILE, TARGET, brief_for, clarified_instruction, literal_plan, micros, referenced_edit, requested_audio,
)
from ..edit_timeline import EditError
from ..instruction_text import QUOTES, replacement_request
from ..render_formats import export_instruction, requests_export
from ..spec_contracts import tool_catalog
from ..workflow_checkpoint import EditBrief, ModelIdentity, PlanAttempt
from ..workflow_context import append_tool_dialog, compact_dialogs, remember_evidence, tool_preview
from ..workflow_shots import bind_clip
from ..workflow_skills import catalog_summary, skill_guidance, skill_identity, use_skill
from ..workflow_state import NeedsInput
from ..workflow_tools import READ_TOOLS, read_tool
from ...infrastructure.persistence.workflow_budget import run_model
from ...infrastructure.providers.workflow_flash import decode_turn, plan_prompt
from . import StageDeps, base_revision, call_tool, pin_index, step, visual_identity


_PLAN_FIELDS = {
    "query", "must_include", "must_exclude", "order_policy", "audio",
    "duration_target_us", "duration_tolerance_us", "replace_last",
}


def run_plan(deps: StageDeps, row):
    cp = row.checkpoint
    if not cp.spec.scope.media_revision_ids:
        raise NeedsInput("项目中还没有可用素材，请上传视频后新建任务。")
    decision = cp.attempt.route_decision or {}
    route = decision.get("plan", [])
    if cp.attempt.intent != "EDIT":
        identity = visual_identity(deps.planner) if cp.attempt.intent == "INSPECT" or "INSPECT" in route else None
        if identity and cp.spec.model_identity is None:
            return step("MATERIAL", spec=cp.spec.pin_model(ModelIdentity.model_validate(identity)))
        return step("MATERIAL")
    message = clarified_instruction(cp.spec.message, cp.attempt.clarification)
    exporting = "EXPORT" in route if decision else requests_export(message)
    container = "mp4"
    if exporting:
        message, container = export_instruction(message, cp.attempt.clarification)
    replacement = replacement_request(message)
    if replacement and not cp.attempt.target_clip_ids:
        base = base_revision(deps, row)
        if not base:
            raise NeedsInput("请选择需要修改的剪辑版本。")
        return step("PLAN", attempt=PlanAttempt(**bind_clip(base, replacement[0])))
    plan = literal_plan(message)
    alternative = bool(
        replacement
        and re.match(r"(?:另一个|另一段|其他|别的)(?:片段|镜头)?(?:[，,。\s]|$)", replacement[1])
    )
    if plan is None and alternative and not cp.attempt.parent_selection:
        raise NeedsInput("请描述替换片段的动作、台词或原片区间。")
    if plan is None and (cp.attempt.selected_candidate or cp.attempt.base_id or requests_export(message)):
        plan = referenced_edit(message, candidate=bool(cp.attempt.selected_candidate))
        if plan and plan["command"] in {"adjust_clip", "render_current"} and not cp.attempt.base_id:
            raise NeedsInput("请先选择已有剪辑版本，或指定候选、原片台词或区间。")
    if plan is None and cp.attempt.selected_candidate:
        raise NeedsInput(
            "请说明如何裁剪选中的片段，例如“提前2秒开始，最多8秒，保留原声”。额外的事件条件需要先核查原片。"
        )
    if plan is None and alternative:
        plan = dict(command="semantic", query=cp.attempt.parent_selection, alternative=True)
        identity = visual_identity(deps.planner)
        if identity and cp.spec.model_identity is None:
            return step("PLAN", spec=cp.spec.pin_model(ModelIdentity.model_validate(identity)))
    if plan is None and "SEARCH" in route and not visual_identity(deps.planner):
        plan = dict(command="semantic", query=cp.attempt.clarification or decision.get("query") or message)
    if plan is None:
        prompt = cp.spec.message + ("\n补充：" + message if cp.attempt.clarification else "")
        plan = model_plan(deps, row, prompt)
        if "state" in plan:
            return plan
    if plan.get("replace_last") and not replacement:
        raise NeedsInput("请明确要替换时间线的哪一段。")
    if plan["command"] == "semantic":
        instructions = QUOTES.sub("", message)
        if match := TARGET.search(instructions):
            plan["duration_target_us"] = micros(match[1])
        if audio := requested_audio(message):
            plan["audio"] = audio
        if re.search(r"按(?:原片|时间|原素材)(?:时间)?顺序", instructions):
            plan["order_policy"] = "SOURCE_ORDER"
    if len(cp.spec.scope.media_revision_ids) > 1 and plan["command"] == "ranges":
        raise NeedsInput("时间区间需要对应一份原片，请选定单个素材后发起剪辑。")
    profile = PROFILE
    if snap := base_revision(deps, row):
        profile = snap["output_profile"]
    if replacement:
        inherited = cp.attempt.parent_brief or {}
        for constraint in ("duration_target_us", "duration_tolerance_us", "order_policy"):
            if constraint in inherited:
                plan.setdefault(constraint, inherited[constraint])
        for constraint in ("must_exclude", "must_include"):
            if inherited.get(constraint):
                plan[constraint] = list(dict.fromkeys(inherited[constraint] + plan.get(constraint, [])))
        plan["preserve_audio"] = not re.search(AUDIO, QUOTES.sub("", message))
    brief = brief_for(row, plan, profile)
    return step(
        "MATERIAL",
        spec=cp.spec.bind_brief(EditBrief.model_validate(brief)),
        attempt=PlanAttempt(
            plan=plan,
            export_requested=exporting,
            export_format=container,
        ),
        docs={"brief": brief},
    )


def model_plan(deps: StageDeps, row, message):
    cp = row.checkpoint
    identity = deps.planner.identity()
    pinned = cp.spec.model_identity
    if pinned is None:
        return step("PLAN", spec=cp.spec.pin_model(ModelIdentity.model_validate(identity)))
    if identity["mode"] != pinned.mode:
        raise EditError(409, "MODEL_CHANGED", "续接工具对话需要兼容的模型工具协议")
    if spec := pin_index(deps, row):
        return step("PLAN", spec=spec)
    scope = cp.spec.scope.model_dump(mode="json")
    catalog = tool_catalog()
    phase = row.state["phase"]
    skill_name = "timeline-refinement" if cp.attempt.target_clip_ids else "grounded-editing"
    skill, defs, allowed = use_skill(deps.repo, row, skill_name, READ_TOOLS)
    turns = cp.attempt.dialogs or plan_prompt(message, scope)
    if not cp.attempt.dialogs:
        turns[0]["content"] += skill_guidance(skill)
        turns[0]["content"] += (
            " Prepare the search query and editing constraints in the user's language. "
            "Use a read tool only to resolve a reference to prior material. The material stage "
            "searches and verifies the picture, so a missing candidate is not a planning failure. "
            "Keep the described event in query, and put only explicit additional constraints in the arrays. "
            "Return the FINAL response as compact JSON without a Markdown fence."
        )
        turns[1]["content"] = json.dumps(
            dict(
                json.loads(turns[1]["content"]),
                available_skills=catalog_summary(),
                index_generation_id=cp.spec.index_generation_id,
                base_revision_id=cp.attempt.base_id,
                target_clip_ids=cp.attempt.target_clip_ids,
            ),
            ensure_ascii=False,
        )
    if identity["mode"] == "json" and not cp.attempt.dialogs:
        turns[0]["content"] += " Read tool definition: " + json.dumps(defs, ensure_ascii=False)
    before = len(turns)
    turns = compact_dialogs(turns, cp, limit=64000, tools=defs)
    if len(turns) < before:
        deps.repo.record_activity(
            row, "context.compacted", messages_before=before, messages_after=len(turns)
        )
    number = cp.attempt.model_round
    if number >= 6:
        raise NeedsInput("模型在限定轮次内未能给出可执行的剪辑方案，请提供目标台词或时间区间。")
    final_only = number >= 2 or cp.attempt.format_repaired
    if final_only:
        defs = []
        turns = [*turns, dict(role="user", content="Return the FINAL plan as a JSON object now.")]
    body = run_model(
        deps.repo,
        row,
        f"plan-{cp.attempt.turn}-{number}",
        turns,
        defs,
        deps.planner.env,
        lambda: deps.planner.request(turns, identity, defs, deps.repo.remaining(row)),
        model=identity["model"],
        identity=identity,
        snapshot={"skill": skill_identity(skill)},
    )
    try:
        msg = body["choices"][0]["message"]
        plan, calls = decode_turn(msg, identity["mode"], catalog, phase, allowed)
        if final_only and plan is None:
            raise ValueError("final plan required")
        seen = cp.attempt.native_call_ids
        if identity["mode"] == "native" and any(call_id in seen for call_id, *_ in calls):
            raise ValueError("reused provider call id")
        if plan is not None:
            if "question" in plan:
                raise NeedsInput(str(plan["question"])[:1000])
            if not isinstance(plan.get("query"), str) or not plan["query"].strip():
                raise ValueError("missing search query")
            plan = {key: value for key, value in plan.items() if key in _PLAN_FIELDS}
            if type(plan.get("replace_last", False)) is not bool:
                raise ValueError("invalid replacement policy")
            plan["command"] = "semantic"
            EditBrief.model_validate(brief_for(row, plan, PROFILE))
            return plan
    except (IndexError, KeyError, TypeError, ValueError):
        if cp.attempt.format_repaired:
            raise EditError(422, "MODEL_FORMAT", "模型连续两次返回无效格式")
        correction = dict(role="user", content="Please return one valid response using the agreed schema.")
        return step("PLAN", attempt=PlanAttempt(
            model_round=number + 1, format_repaired=True, dialogs=turns + [correction],
        ))
    assistant = dict(role="assistant", content=msg.get("content"))
    if identity["mode"] == "native":
        assistant["tool_calls"] = msg["tool_calls"]
    turns = list(turns) + [assistant]
    references = cp.context.references
    for i, (call_id, name, args, rejected) in enumerate(calls):
        value = rejected
        if value is None:
            try:
                value = call_tool(
                    deps, row,
                    f"round-{number}-{i}",
                    name,
                    args,
                    lambda: read_tool(deps.repo, deps.search, row, name, args, references=references),
                )
                ids = list(value.get("evidence_ids", ()))
                ids.extend(e for hit in value.get("hits", ()) for e in hit["evidence_ids"])
                references = remember_evidence(deps.repo, row, references, ids, name)
            except (HTTPException, EditError, ValueError):
                # Every native request receives its own non-enumerating result.
                value = {"error": "TOOL_DENIED"}
            value, clipped = tool_preview(name, value)
            if clipped or value.get("excerpts"):
                deps.repo.record_activity(
                    row,
                    "context.tool_projection",
                    tool=name,
                    truncated=clipped,
                    excerpts=len(value.get("excerpts", [])),
                )
            if clipped:
                value["truncated_fields"] = clipped
        append_tool_dialog(turns, identity["mode"], call_id, value)
    return step(
        "PLAN",
        attempt=PlanAttempt(
            dialogs=turns,
            model_round=number + 1,
            native_call_ids=seen + [call[0] for call in calls] if identity["mode"] == "native" else [],
        ),
        references=references,
    )
