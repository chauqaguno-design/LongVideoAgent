"""One resumable stage per claim. Slow media work uses a revision-bound render job."""

import json
import re

from fastapi import HTTPException
from sqlalchemy import select
from dovideo.application.edit_instructions import (
    PROFILE,
    REPLACE_LAST,
    append_ranges,
    brief_for,
    clarified_instruction,
    choose_intervals,
    exclude_subtitles,
    export_instruction,
    literal_plan,
    referenced_edit,
    relative_window,
    requests_export,
)
from dovideo.application.edit_timeline import EditError
from dovideo.application.media_sampling import segment_bounds
from dovideo.application.spec_contracts import tool_catalog, validate_contract
from dovideo.application.workflow_state import NeedsInput, stable_id
from dovideo.application.workflow_context import compact_dialogs
from dovideo.application.workflow_tools import READ_TOOLS, read_tool
from dovideo.infrastructure.persistence.edit_repository import job_view
from dovideo.infrastructure.persistence.edit_tables import jobs, revisions
from dovideo.infrastructure.persistence.workflow_budget import run_model
from dovideo.infrastructure.providers.workflow_flash import decode_turn, plan_prompt
from dovideo.application.workflow_inspections import inspection


def step(phase, *, checkpoint=None, status="RUNNING", event="PROGRESS", payload=None, docs=None, **state):
    return dict(
        state=dict(phase=phase, status=status, **state),
        checkpoint=checkpoint or {},
        event=event,
        payload=payload,
        docs=docs or {},
    )


def query_for(scope, text):
    mode = (
        "COUNT"
        if any(x in text for x in ("几次", "多少次", "次数"))
        else (
            "ALL"
            if any(x in text for x in ("所有", "全部", "每一次"))
            else "FIRST" if any(x in text for x in ("首次", "第一次", "最早")) else "LOCAL"
        )
    )
    return dict(scope=scope, text=text, query_mode=mode, top_k=12)


def visual_candidates(answer):
    # A verified line of dialogue is still only a lead for a question about action.
    return dict(
        answer,
        answer="尚未证实所问画面。以下是可以回看的候选依据。",
        claims=[
            dict(claim, support="UNCERTAIN") if claim["support"] == "SUPPORTED" else claim
            for claim in answer["claims"]
        ],
    )


class WorkflowSteps:
    def __init__(self, repo, search, planner, render_worker=None):
        self.repo, self.edits, self.search, self.planner = repo, repo.edits, search, planner
        self.render_worker = render_worker

    def tool(self, row, step_key, name, args, execute):
        key = f"{row.checkpoint['turn']}:{step_key}"
        return self.repo.invoke(row, key, name, args, execute)

    def visual_identity(self):
        if self.planner.env.get("DOVIDEO_WORKFLOW_LIVE_MODEL") != "1":
            return None
        try:
            return self.planner.identity(vision=True)
        except EditError as exc:
            if exc.code in {"VISION_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"}:
                return None
            raise

    def model_plan(self, row, message):
        cp = row.checkpoint
        identity = self.planner.identity()
        pinned = cp.get("model_identity")
        if pinned is None:
            return step("PLAN", checkpoint={"model_identity": identity})
        if identity != pinned:
            raise EditError(409, "MODEL_CHANGED", "原任务依赖的模型版本或协议已改变")
        scope = cp["scope"]
        catalog = tool_catalog()
        role = "COORDINATOR"
        allowed = READ_TOOLS
        wires = {catalog.tools[name].provider_name for name in allowed}
        defs = [tool for tool in catalog.provider_tools(role) if tool["function"]["name"] in wires]
        turns = cp.get("dialogs") or plan_prompt(message, scope)
        if identity["mode"] == "json" and not cp.get("dialogs"):
            turns[0]["content"] += " Read tool definition: " + json.dumps(defs, ensure_ascii=False)
        turns = compact_dialogs(turns, cp, limit=64000)
        number = cp.get("model_round", 0)
        if number >= 6:
            raise NeedsInput("模型在限定轮次内未能给出可执行的剪辑方案，请提供目标台词或时间区间。")
        body = run_model(
            self.repo,
            row,
            f"plan-{cp['turn']}-{number}",
            turns,
            defs,
            self.planner.env,
            lambda: self.planner.request(turns, identity, defs, self.repo.remaining(row)),
            model=identity["model"],
        )
        try:
            msg = body["choices"][0]["message"]
            plan, calls = decode_turn(msg, identity["mode"], catalog, role, allowed)
            seen = cp.get("native_call_ids", [])
            if identity["mode"] == "native" and any(call_id in seen for call_id, *_ in calls):
                raise ValueError("reused provider call id")
            if plan is not None:
                if "question" in plan:
                    raise NeedsInput(str(plan["question"])[:1000])
                if not isinstance(plan.get("query"), str) or not plan["query"].strip():
                    raise ValueError("missing search query")
                plan = {
                    k: v
                    for k, v in plan.items()
                    if k
                    in (
                        "query",
                        "must_include",
                        "must_exclude",
                        "order_policy",
                        "audio",
                        "duration_target_us",
                        "duration_tolerance_us",
                        "replace_last",
                    )
                }
                if type(plan.get("replace_last", False)) is not bool:
                    raise ValueError("invalid replacement policy")
                plan["command"] = "semantic"
                brief_for(row, plan, PROFILE)
                return plan
        except (IndexError, KeyError, TypeError, ValueError):
            if cp.get("format_repaired"):
                raise EditError(422, "MODEL_FORMAT", "模型连续两次返回无效格式")
            return step(
                "PLAN",
                checkpoint=dict(
                    model_round=number + 1,
                    format_repaired=True,
                    dialogs=turns
                    + [
                        {
                            "role": "user",
                            "content": "Please return one valid response using the agreed schema.",
                        }
                    ],
                ),
            )
        assistant = dict(role="assistant", content=msg.get("content"))
        if identity["mode"] == "native":
            assistant["tool_calls"] = msg["tool_calls"]
        turns = list(turns) + [assistant]
        for i, (call_id, name, args, rejected) in enumerate(calls):
            value = rejected
            if value is None:
                try:
                    value = self.tool(
                        row,
                        f"round-{number}-{i}",
                        name,
                        args,
                        lambda: read_tool(self.repo, self.search, row, name, args),
                    )
                    catalog.tools[name].validate(value, output=True)
                except (HTTPException, EditError, ValueError):
                    # Every native request receives its own non-enumerating result.
                    value = {"error": "TOOL_DENIED"}
                if "hits" in value:
                    value = {
                        "query_id": value["query_id"],
                        "hits": value["hits"][:4],
                        "warnings": value["warnings"][:3],
                    }
            content = json.dumps(value, ensure_ascii=False)
            if identity["mode"] == "native":
                turns.append(dict(role="tool", tool_call_id=call_id, content=content))
            else:
                turns.append(
                    dict(
                        role="user",
                        content=json.dumps({"untrusted_tool_result": content}, ensure_ascii=False),
                    )
                )
        return step(
            "PLAN",
            checkpoint=dict(
                dialogs=turns,
                model_round=number + 1,
                native_call_ids=seen + [call[0] for call in calls] if identity["mode"] == "native" else [],
            ),
        )

    def plan(self, row):
        cp = row.checkpoint
        if not cp["scope"]["media_revision_ids"]:
            raise NeedsInput("项目中还没有可用素材，请上传视频后新建任务。")
        if (
            cp["team"]
            and self.planner.env.get("DOVIDEO_WORKFLOW_LIVE_MODEL") == "1"
            and not cp.get("model_identity")
        ):
            return step("PLAN", checkpoint={"model_identity": self.planner.identity()})
        decision = cp.get("route_decision") or {}
        route = decision.get("plan", [])
        if cp["intent"] != "EDIT":
            identity = self.visual_identity() if cp["intent"] == "INSPECT" or "INSPECT" in route else None
            return step("MATERIAL", checkpoint={"model_identity": identity} if identity else {})
        message = clarified_instruction(cp["message"], cp.get("clarification"))
        exporting = "EXPORT" in route if decision else requests_export(message)
        container = "mp4"
        if exporting:
            message, container = export_instruction(message, cp.get("clarification"))
        plan = literal_plan(message)
        if plan is None and (cp.get("selected_candidate") or cp.get("base_id") or requests_export(message)):
            plan = referenced_edit(message, candidate=bool(cp.get("selected_candidate")))
            if plan and plan["command"] in {"adjust_clip", "render_current"} and not cp.get("base_id"):
                raise NeedsInput("请先选择已有剪辑版本，或指定候选、原片台词或区间。")
        if plan is None and cp.get("selected_candidate"):
            raise NeedsInput(
                "请说明如何裁剪选中的片段，例如“提前2秒开始，最多8秒，保留原声”。额外的事件条件需要先核查原片。"
            )
        if plan is None and "SEARCH" in route and not self.visual_identity():
            plan = dict(command="semantic", query=cp.get("clarification") or decision.get("query") or message)
        if plan is None:
            plan = self.model_plan(
                row, cp["message"] + ("\n补充：" + message if cp.get("clarification") else "")
            )
            if "state" in plan:
                return plan
        if plan["command"] == "semantic" and REPLACE_LAST.search(message):
            plan["replace_last"] = True
        if plan.get("replace_last") and not cp.get("base_id"):
            raise NeedsInput("请先选定需要替换末段的已有剪辑版本。")
        if len(cp["scope"]["media_revision_ids"]) > 1 and plan["command"] == "ranges":
            raise NeedsInput("时间区间需要对应一份原片，请选定单个素材后发起剪辑。")
        profile = PROFILE
        if "edit_id" in cp:
            snap = self.edits.get(
                revisions, row.workspace_id, cp["base_id"], lambda x: x.snapshot, edit_id=cp["edit_id"]
            )
            profile = snap["output_profile"]
        brief = brief_for(row, plan, profile)
        return step(
            "MATERIAL",
            checkpoint=dict(
                plan=plan,
                brief=brief,
                export_requested=exporting,
                export_format=container,
            ),
            docs={"brief": brief},
        )

    def dialogue_intervals(self, row, query_id):
        cp, workspace = row.checkpoint, row.workspace_id
        plan = cp["plan"]
        request = dict(
            scope=cp["scope"],
            text=f"{plan.get('text_source', '台词')}：{plan['query']}",
            query_mode="ALL",
            top_k=12,
        )
        answer = self.tool(
            row,
            "dialogue",
            "video.search_evidence",
            request,
            lambda: self.search.answer(workspace, query_id, request),
        )
        result_id = answer["complete_event_list_artifact_id"]
        if excluded := plan.get("must_exclude"):
            # All constraints use the same immutable index as the positive quotation.
            files = self.search.files
            manifest = files.get(workspace, result_id, project=row.project_id)
            with files.path(manifest).open(encoding="utf8") as stream:
                scan = json.load(stream)
            media = cp["scope"]["media_revision_ids"]
            generation = scan["index_generation_id"]
            _, rows = self.search.catalog.search_scope(
                workspace, row.project_id, media, generation_id=generation
            )
            covered = self.search.catalog.literal_coverage(workspace, row.project_id, generation, media)
            hits = exclude_subtitles(scan["events"], excluded, rows, covered)
        else:
            hits = self.search.files.occurrences(workspace, row.project_id, result_id)
        if plan.get("require_audio") and hits:
            audible_media = {
                mid
                for mid in {h["media_revision_id"] for h in hits}
                if any(s["kind"] == "audio" for s in self.repo.sources.media(workspace, mid)["streams"])
            }
            hits = [h for h in hits if h["media_revision_id"] in audible_media]
            if not hits:
                raise NeedsInput(
                    "匹配的素材没有原音轨，无法保留讲话。请选择带声音的素材或调整音轨要求。",
                    kind="CONSTRAINT_CONFLICT",
                )
        if plan.get("first"):
            hits = sorted(
                hits,
                key=lambda h: (
                    cp["scope"]["media_revision_ids"].index(h["media_revision_id"]),
                    h["range"]["start_us"],
                ),
            )[:1]
        intervals = [(h["media_revision_id"], h["range"], h["evidence_ids"]) for h in hits]
        if not intervals:
            raise NeedsInput("没有找到可以核对的原片台词，请用引号输入原句或直接给出起止时间。")
        return intervals

    def referenced_interval(self, row):
        cp, workspace = row.checkpoint, row.workspace_id
        plan, candidate = cp["plan"], cp.get("selected_candidate")
        original = None
        if plan["command"] == "candidate":
            if not candidate or not candidate["evidence_ids"]:
                raise NeedsInput("请先选择带原片依据的候选片段。")
            mid, span, ids = candidate["media_revision_id"], candidate["range"], candidate["evidence_ids"]
        else:
            timeline = self.edits.get(
                revisions, workspace, cp["base_id"], lambda r: r.snapshot, edit_id=cp["edit_id"]
            )
            clips = timeline["clips"]
            number = len(clips) if plan.get("last") else plan.get("clip_number", 1 if len(clips) == 1 else 0)
            if not 1 <= number <= len(clips):
                raise NeedsInput("请说明要修改时间线中的第几段，或指定最后一段。")
            original = clips[number - 1]
            mid, ids = original["media_revision_id"], original["evidence_ids"]
            span = dict(start_us=original["source_start_us"], end_us=original["source_end_us"])
        media, proofs = self.source_proofs(row, mid, span, ids)
        if candidate and span["end_us"] - span["start_us"] <= 1:
            start, end = segment_bounds(span["start_us"], media["duration_us"])
            span = dict(
                start_us=(
                    span["start_us"] if any(k in plan for k in ("before_us", "after_us", "max_us")) else start
                ),
                end_us=end,
            )
        window = relative_window(span, media["duration_us"], plan)
        retained = [
            eid
            for eid, p in proofs.items()
            if p["range"]["start_us"] < window["end_us"] and p["range"]["end_us"] > window["start_us"]
        ]
        if proofs and not retained:
            raise NeedsInput(
                "调整后的区间未包含选中的原片依据，请修改起点或时长。", kind="CONSTRAINT_CONFLICT"
            )
        return [(mid, window, retained)], original

    def source_proofs(self, row, mid, span, ids):
        if mid not in row.checkpoint["scope"]["media_revision_ids"]:
            raise EditError(404, "NOT_FOUND", "所选来源不在本次任务范围内")
        media = self.repo.sources.media(row.workspace_id, mid)
        if not 0 <= span["start_us"] < span["end_us"] <= media["duration_us"]:
            raise NeedsInput("所选区间已失效，请重新选择原片依据。")
        proofs = {eid: self.edits.resolve_evidence(row.workspace_id, eid) for eid in ids}
        for proof in proofs.values():
            if (
                proof["workspace_id"] != row.workspace_id
                or proof["media_revision_id"] != mid
                or proof["project_id"] != row.project_id
                or proof["range"]["end_us"] <= span["start_us"]
                or proof["range"]["start_us"] >= span["end_us"]
            ):
                raise EditError(404, "NOT_FOUND", "所选依据与原片区间不一致")
        return media, proofs

    def ask_with_candidates(self, answer, question):
        refs = list(dict.fromkeys(e for c in answer["claims"] for e in c["evidence_ids"]))
        return step(
            "MATERIAL",
            status="AWAITING_INPUT",
            event="NEEDS_INPUT",
            pending_job_ids=[],
            evidence_ids=refs,
            awaiting_input=NeedsInput(question).input,
            docs={"result": answer},
        )

    def material(self, row):
        cp = row.checkpoint
        workspace = row.workspace_id
        # Search snapshots are immutable. A clarification can issue a new query in the same workflow.
        query_id = stable_id(row.id, f"query:{cp['turn']}") if cp["turn"] else row.id
        if cp["intent"] != "EDIT" or cp.get("plan", {}).get("command") in ("quote", "semantic"):
            pending = []
            for mid in cp["scope"]["media_revision_ids"]:
                asset = self.repo.sources.media(workspace, mid)["asset_id"]
                history = self.search.catalog.page(workspace, asset, "RUN", None, 100)["items"]
                if not any(job["index_generation_id"] for job in history):
                    pending.extend(
                        j["processing_run_id"] for j in history if j["status"] in ("QUEUED", "RUNNING")
                    )
            if pending:
                if sorted(pending) == sorted(row.state["pending_job_ids"]):
                    return {"sleep": True}
                return step(
                    "MATERIAL",
                    status="WAITING_JOB",
                    pending_job_ids=pending,
                    checkpoint=dict(
                        processing_job_ids=list(dict.fromkeys(cp.get("processing_job_ids", []) + pending))
                    ),
                )
        if cp["intent"] != "EDIT":
            decision = cp.get("route_decision") or {}
            question = cp.get("clarification") or decision.get("query") or cp["message"]
            req = cp.get("search_request") or query_for(cp["scope"], question)
            selected = cp.get("selected_candidate")
            selected_proofs = {}
            if selected:
                mid, span = selected["media_revision_id"], selected["range"]
                _, selected_proofs = self.source_proofs(row, mid, span, selected["evidence_ids"])
                req = dict(req, scope=dict(project_id=row.project_id, media_revision_ids=[mid]), range=span)
            answer = self.tool(
                row,
                "answer",
                "video.search_evidence",
                req,
                lambda: self.search.answer(workspace, query_id, req),
            )
            answer = dict(answer, run_id=row.id)
            if selected_proofs and re.search(
                r"(?:有什么|有哪些|查看|看看|原始)(?:证据|依据)[？?。！!\s]*$", question
            ):
                claims = [
                    dict(
                        text="原片记录：" + p["claim"],
                        support=selected.get("support", "UNCERTAIN"),
                        evidence_ids=[eid],
                    )
                    for eid, p in selected_proofs.items()
                ]
                answer.update(answer="所选候选的原片记录如下。", claims=claims)
                validate_contract("AnswerResult", answer)
                return step(
                    "REVIEW",
                    checkpoint={"answer": answer},
                    pending_job_ids=[],
                    evidence_ids=list(selected_proofs),
                    event="EVIDENCE",
                )
            needs_visual = cp["intent"] == "INSPECT" or "INSPECT" in decision.get("plan", [])
            visual_supported = (
                not req.get("text")
                and answer["coverage_summary"]["protocol_id"] == "exact-rgb-v1"
                and any(c["support"] == "SUPPORTED" for c in answer["claims"])
            )
            if needs_visual and not visual_supported and req["query_mode"] == "LOCAL":
                answer = visual_candidates(answer)
            if (
                needs_visual
                and not visual_supported
                and req["query_mode"] == "LOCAL"
                and self.visual_identity()
                and (
                    "INSPECT" in decision.get("plan", [])
                    or not any(c["support"] == "SUPPORTED" for c in answer["claims"])
                )
            ):
                result = self.tool(
                    row,
                    "inspect-candidates",
                    "video.search_evidence",
                    req,
                    lambda: self.search.search(workspace, req),
                )
                inspected = []
                for n, hit in enumerate(result["hits"][:3]):
                    mid = hit["media_revision_id"]
                    report = inspection(
                        row,
                        f"inspect-{n}",
                        mid,
                        hit["range"],
                        req.get("text", "与查询画面一致"),
                        generation=result["index_generation_id"],
                    )
                    if "evidence_id" in report:
                        inspected.append(
                            dict(
                                text=report["reason"],
                                support=report["support"],
                                evidence_ids=[report["evidence_id"]],
                            )
                        )
                if inspected:
                    visual_supported = any(c["support"] == "SUPPORTED" for c in inspected)
                    answer = dict(
                        answer,
                        claims=inspected + answer["claims"],
                        answer="；".join(c["text"] for c in inspected),
                        warnings=[*answer["warnings"], "时序复核仅覆盖列明的候选片段。"],
                    )
            validate_contract("AnswerResult", answer)
            if decision and req["query_mode"] == "LOCAL":
                if needs_visual and not visual_supported:
                    return self.ask_with_candidates(
                        answer, "现有证据不足以确认所问画面。请选择候选回看，或提供同期台词和原片区间。"
                    )
                if not any(c["support"] == "SUPPORTED" for c in answer["claims"]):
                    return self.ask_with_candidates(
                        answer, "目前只有候选，没有足够依据回答。请指定一个候选、原片区间或提供确切台词。"
                    )
            proof = list(dict.fromkeys(e for c in answer["claims"] for e in c["evidence_ids"]))
            return step(
                "REVIEW",
                checkpoint={"answer": answer},
                pending_job_ids=[],
                evidence_ids=proof,
                event="EVIDENCE",
            )
        plan = cp["plan"]
        original = None
        if plan["command"] == "quote":
            intervals = self.dialogue_intervals(row, query_id)
        elif plan["command"] == "semantic":
            request = query_for(cp["scope"], plan["query"])
            if not self.visual_identity():
                answer = self.tool(
                    row,
                    "edit-candidates",
                    "video.search_evidence",
                    request,
                    lambda: self.search.answer(workspace, query_id, request),
                )
                return self.ask_with_candidates(
                    dict(visual_candidates(answer), run_id=row.id),
                    "当前无法核实要求的画面。可在候选中选定要剪的片段，或给出原片台词和区间。",
                )
            result = self.tool(
                row,
                "search",
                "video.search_evidence",
                request,
                lambda: self.search.search(workspace, request),
            )
            # An existing verified subtitle or claim might not establish this user's visual brief.
            hits = []
            for n, hit in enumerate(result["hits"][:3]):
                mid = hit["media_revision_id"]
                observation_ids = [
                    observation
                    for eid in hit["evidence_ids"]
                    for observation in self.edits.resolve_evidence(workspace, eid)["observation_ids"]
                ]
                report = inspection(
                    row,
                    f"candidate-{n}",
                    mid,
                    hit["range"],
                    cp["brief"],
                    generation=result["index_generation_id"],
                    underlying=observation_ids,
                )
                if report["support"] == "SUPPORTED":
                    hits.append(dict(hit, verified=True, evidence_ids=[report["evidence_id"]]))
            if not hits:
                raise NeedsInput(
                    "尚未找到经过核实的对应画面或台词。可以改用明确的起止时间，或补充更具体的原片台词。"
                )
            if cp["brief"]["must_exclude"]:
                raise NeedsInput(
                    "现有证据不足以验证排除条件，请确认可用的片段区间。", kind="CONSTRAINT_CONFLICT"
                )
            if plan.get("first") or cp["brief"]["order_policy"] == "SOURCE_ORDER":
                hits.sort(
                    key=lambda h: (
                        cp["scope"]["media_revision_ids"].index(h["media_revision_id"]),
                        h["range"]["start_us"],
                    )
                )
            if plan.get("first"):
                hits = hits[:1]
            intervals = [(h["media_revision_id"], h["range"], h["evidence_ids"]) for h in hits]
        elif plan["command"] in ("candidate", "adjust_clip"):
            intervals, original = self.referenced_interval(row)
        elif plan["command"] == "ranges":
            mid = cp["scope"]["media_revision_ids"][0]
            length = self.repo.sources.media(workspace, mid)["duration_us"]
            if any(r["start_us"] >= r["end_us"] or r["end_us"] > length for r in plan["ranges"]):
                raise NeedsInput(f"选区超出原片或起止顺序有误，原片长 {length/1e6:g} 秒。")
            intervals = [(mid, span, []) for span in plan["ranges"]]
        else:
            intervals = []
        if plan["command"] in ("quote", "semantic"):
            intervals = choose_intervals(cp["brief"], intervals)
            if len(intervals) > 200:
                raise NeedsInput("匹配超过一条时间线可容纳的 200 段，请缩小选区或指定首次出现。")
        proof = list(dict.fromkeys(e for _, _, ids in intervals for e in ids))
        return step(
            "PROPOSE",
            checkpoint={"intervals": intervals, "original_clip": original},
            pending_job_ids=[],
            evidence_ids=proof,
            event="EVIDENCE",
        )

    def propose(self, row):
        cp = row.checkpoint
        plan = cp["plan"]
        if plan["command"] == "render_current":
            if not cp.get("base_id"):
                raise NeedsInput("请先选定要导出的剪辑版本。")
            operations = []
        elif plan["command"] in ("quote", "semantic", "ranges", "candidate", "adjust_clip"):
            operations = append_ranges(cp["brief"], cp["intervals"])
            if original := cp.get("original_clip"):
                clip = dict(original, **operations[0]["clip"])
                clip_id = clip.pop("clip_id")
                if "audio" not in plan:
                    clip["audio_policy"] = original["audio_policy"]
                operations = [dict(op="replace_clip", clip_id=clip_id, clip=clip)]
            if plan.get("replace_last"):
                if not cp.get("base_id"):
                    raise NeedsInput("请先选择已有的剪辑时间线。")
                snap = self.edits.get(revisions, row.workspace_id, cp["base_id"], lambda r: r.snapshot)
                if not snap["clips"]:
                    raise NeedsInput("当前版本没有可以替换的末段。")
                operations[0] = dict(
                    op="replace_clip", clip_id=snap["clips"][-1]["clip_id"], clip=operations[0]["clip"]
                )
        else:
            if not cp.get("edit_id"):
                raise NeedsInput("请在已有剪辑方案中选择要修改的时间线。")
            snap = self.edits.get(
                revisions, row.workspace_id, cp["base_id"], lambda r: r.snapshot, edit_id=cp["edit_id"]
            )
            ids = [c["clip_id"] for c in snap["clips"]]
            if plan["command"] == "restore":
                with self.repo.sessions() as db:
                    key = db.execute(
                        select(revisions.c.id).where(
                            revisions.c.edit_id == cp["edit_id"],
                            revisions.c.workspace_id == row.workspace_id,
                            revisions.c.number == plan["number"],
                        )
                    ).scalar()
                if not key:
                    raise NeedsInput("这个剪辑方案没有指定的版本，请核对版本号。")
                target = self.edits.get(revisions, row.workspace_id, key, lambda r: r.snapshot)
                if any(
                    c["media_revision_id"] not in cp["scope"]["media_revision_ids"] for c in target["clips"]
                ):
                    raise NeedsInput("要恢复的版本使用了本次范围之外的素材，请选定完整原片后重新提交。")
                operations = [dict(op="restore_revision", source_revision_id=key)]
            elif not ids:
                raise NeedsInput("这个版本中还没有片段。")
            elif plan["command"] == "remove_last":
                operations = [dict(op="remove_clip", clip_id=ids[-1])]
            else:
                operations = [dict(op="reorder_clips", clip_ids=list(reversed(ids)))]
        proposal = dict(base_revision=cp.get("base", 0), operations=operations, brief_id=cp["brief_id"])
        return step("COMMIT", checkpoint={"proposal": proposal}, docs={"proposal": proposal})

    def commit(self, row):
        cp = row.checkpoint
        if cp["plan"]["command"] == "render_current":
            return step("RENDER", checkpoint=dict(revision_id=cp["base_id"]))
        scope = row.workspace_id
        fence = lambda db: self.repo.writer_guard(db, row, "COMMIT")
        edit_id = cp.get("edit_id")
        if not edit_id:
            body = dict(
                project_id=row.project_id,
                title=cp["message"][:120],
                output_profile=cp["brief"]["output_profile"],
                idempotency_key=f"{row.id}-project",
            )
            edit = self.tool(
                row, "create", "edit.create", body, lambda: self.edits.create(scope, body, guard=fence)
            )
            edit_id = edit["edit_project_id"]
        proposal = cp["proposal"]
        body = dict(
            operations=proposal["operations"],
            expected_revision=proposal["base_revision"],
            reason=cp["message"][:1500],
            idempotency_key=f"{row.id}-{cp['turn']}-patch",
        )
        validate_contract("TimelinePatch", body)
        receipt = self.tool(
            row,
            "patch",
            "edit.apply_patch",
            body,
            lambda: self.edits.patch(scope, edit_id, body, guard=fence, brief_id=cp["brief_id"]),
        )
        return step("RENDER", checkpoint=dict(edit_id=edit_id, revision_id=receipt["revision_id"]))

    def render(self, row):
        cp = row.checkpoint
        mid = cp["revision_id"]
        revision = self.edits.get(revisions, row.workspace_id, mid, lambda r: r.snapshot)
        if not revision["clips"]:
            if cp.get("export_requested"):
                raise NeedsInput("时间线当前没有片段，请添加片段后导出。")
            return step("REVIEW")
        exporting = bool(cp.get("export_requested") and cp.get("preview_artifact_id"))
        profile = "EXPORT" if exporting else "PREVIEW"
        job_key = "export_job_id" if exporting else "render_job_id"
        if not row.state["pending_job_ids"]:
            body = dict(
                revision_id=mid, profile=profile, idempotency_key=f"{row.id}-{cp['turn']}-{profile.lower()}"
            )
            job = self.tool(
                row,
                profile.lower(),
                "render.submit",
                body,
                lambda: self.edits.enqueue(
                    row.workspace_id, body, export_format=cp.get("export_format", "mp4") if exporting else "mp4",
                    guard=lambda db: self.repo.writer_guard(db, row, "RENDER")
                ),
            )
            return step(
                "RENDER",
                status="WAITING_JOB",
                pending_job_ids=[job["render_job_id"]],
                checkpoint={job_key: job["render_job_id"]},
            )
        job = self.edits.get(jobs, row.workspace_id, cp[job_key], job_view)
        if job["status"] in ("QUEUED", "RUNNING", "VALIDATING"):
            return {"sleep": True}
        if job["status"] != "SUCCEEDED":
            raise EditError(422, "RENDER_FAILED", "当前渲染作业未能通过成片校验")
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
            return step(
                "REVIEW",
                checkpoint=dict(
                    export_artifact_id=job["artifact_id"], export_qa_id=job["validation_report_id"]
                ),
                pending_job_ids=[],
                payload=trace,
            )
        return step(
            "RENDER" if cp.get("export_requested") else "REVIEW",
            checkpoint=dict(preview_artifact_id=job["artifact_id"], qa_id=job["validation_report_id"]),
            pending_job_ids=[],
            event="PREVIEW_READY",
            payload=trace,
        )

    def review(self, row):
        cp = row.checkpoint
        if cp["intent"] == "EDIT":
            result = dict(
                edit_project_id=cp["edit_id"],
                revision_id=cp["revision_id"],
                brief=cp["brief"],
                render_job_id=cp.get("render_job_id"),
                preview_artifact_id=cp.get("preview_artifact_id"),
                validation_report_id=cp.get("qa_id"),
                checks=dict(media="PASSED" if cp.get("qa_id") else "EMPTY", semantic="NOT_CHECKED"),
                gaps=["成片语义尚未经过视觉模型独立复核。"],
            )
            if (
                cp["plan"]["command"] == "candidate"
                and cp["selected_candidate"].get("support") != "SUPPORTED"
            ):
                result["gaps"].append("本次按你选中的候选时间裁剪，候选中的人物或动作仍待核实。")
            if cp.get("export_artifact_id"):
                result.update(
                    export_artifact_id=cp["export_artifact_id"],
                    export_format=cp.get("export_format", "mp4"),
                    export_validation_report_id=cp["export_qa_id"],
                    export_job_id=cp["export_job_id"],
                )
            if cp["plan"]["command"] == "semantic" and cp.get("preview_artifact_id"):
                revision = self.edits.get(
                    revisions, row.workspace_id, cp["revision_id"], lambda r: r.snapshot
                )
                report = inspection(
                    row,
                    "final-output",
                    None,
                    dict(start_us=0, end_us=revision["total_duration_us"]),
                    cp["brief"],
                    preview=cp.get("export_artifact_id", cp["preview_artifact_id"]),
                )
                result["checks"]["semantic"] = report
                result["gaps"] = ["语义审核只覆盖成片中实际抽取的帧。"]
                if report["support"] != "SUPPORTED":
                    waiting = NeedsInput(
                        "成片结构校验通过，但抽帧语义审核尚未支持目标。请检查预览后调整指令。"
                    )
                    return step(
                        "REVIEW",
                        status="AWAITING_INPUT",
                        awaiting_input=waiting.input,
                        event="NEEDS_INPUT",
                        docs={"result": result},
                    )
        else:
            result = cp["answer"]
        return step("DONE", status="SUCCEEDED", docs={"result": result}, event="COMPLETED")
