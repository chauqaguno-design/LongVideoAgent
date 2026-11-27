"""Find and verify source evidence for a question or an edit."""

import json
import re

from ..edit_instructions import choose_intervals, relative_window
from ..evidence_search import exclude_subtitles
from ..media_sampling import segment_bounds
from ..sparse_retrieval import normalize_text
from ..workflow_checkpoint import PlanAttempt
from ..workflow_identity import generation_key
from ..workflow_inspections import inspection
from ..workflow_shots import candidate, distinct_candidates, local_brief, select_replacement
from ..workflow_skills import use_skill
from ..workflow_state import NeedsInput, stable_id
from ...infrastructure.persistence.edit_tables import revisions
from . import StageDeps, base_revision, call_tool, pin_index, source_proofs, step, visual_identity


def run_material(deps: StageDeps, row):
    cp = row.checkpoint
    # A clarification gets its own query while the source index stays pinned.
    query_id = stable_id(row.id, generation_key(cp, f"query:{cp.attempt.turn}"))
    if cp.attempt.intent != "EDIT" or cp.attempt.plan.get("command") in ("quote", "semantic"):
        pending = []
        for mid in cp.spec.scope.media_revision_ids:
            asset = deps.repo.sources.media(row.workspace_id, mid)["asset_id"]
            history = deps.search.catalog.page(row.workspace_id, asset, "RUN", None, 100)["items"]
            if not any(job["index_generation_id"] for job in history):
                pending.extend(j["processing_run_id"] for j in history if j["status"] in ("QUEUED", "RUNNING"))
        if pending:
            if sorted(pending) == sorted(row.state["pending_job_ids"]):
                return {"sleep": True}
            return step("MATERIAL", status="WAITING_JOB", pending_job_ids=pending, processing_job_ids=pending)
        if spec := pin_index(deps, row):
            return step("MATERIAL", spec=spec, pending_job_ids=[])
        use_skill(deps.repo, row, "evidence-search", {"video.search_evidence"})
    if cp.attempt.intent == "EDIT":
        return _edit_material(deps, row, query_id)
    return _answer_question(deps, row, query_id)


def _query_for(scope, text):
    if any(word in text for word in ("几次", "多少次", "次数")):
        mode = "COUNT"
    elif any(word in text for word in ("所有", "全部", "每一次")):
        mode = "ALL"
    elif any(word in text for word in ("首次", "第一次", "最早")):
        mode = "FIRST"
    else:
        mode = "LOCAL"
    return dict(scope=scope, text=text, query_mode=mode, top_k=12)


def _search_evidence(deps, row, key, request, *, query_id=None):
    """Read candidate hits, or the answer for a particular query, through the same tool ledger."""
    def execute():
        generation = row.checkpoint.spec.index_generation_id
        options = {"generation_id": generation} if generation else {}
        if query_id is None:
            return deps.search.search(row.workspace_id, request, **options)
        return deps.search.answer(row.workspace_id, query_id, request, **options)

    return call_tool(deps, row, key, "video.search_evidence", request, execute)


def _visual_candidates(answer):
    # A verified line of dialogue is still only a lead for a question about action.
    claims = [dict(c, support="UNCERTAIN") if c["support"] == "SUPPORTED" else c for c in answer["claims"]]
    return dict(answer, answer="尚未证实所问画面。以下是可以回看的候选依据。", claims=claims)


def _answer_step(answer, question=None):
    ids = list(dict.fromkeys(e for claim in answer["claims"] for e in claim["evidence_ids"]))
    if question:
        return step("MATERIAL", status="AWAITING_INPUT", event="NEEDS_INPUT", pending_job_ids=[],
                    evidence_ids=ids, awaiting_input=NeedsInput(question).input, docs={"result": answer})
    return step("REVIEW", attempt=PlanAttempt(answer=answer), pending_job_ids=[], evidence_ids=ids, event="EVIDENCE")


def _supported(claims):
    return any(claim["support"] == "SUPPORTED" for claim in claims)


def _answer_question(deps, row, query_id):
    cp = row.checkpoint
    decision = cp.attempt.route_decision or {}
    question = cp.attempt.clarification or decision.get("query") or cp.spec.message
    request = cp.spec.search_request or _query_for(cp.spec.scope.model_dump(mode="json"), question)
    selected = cp.attempt.selected_candidate
    proofs = {}
    if selected:
        mid, span = selected["media_revision_id"], selected["range"]
        _, proofs = source_proofs(deps.repo, row, mid, span, selected["evidence_ids"])
        request = dict(request, scope=dict(project_id=row.project_id, media_revision_ids=[mid]), range=span)
    answer = dict(_search_evidence(deps, row, "answer", request, query_id=query_id), run_id=row.id)
    if proofs and re.search(r"(?:有什么|有哪些|查看|看看|原始)(?:证据|依据)[？?。！!\s]*$", question):
        support = selected.get("support", "UNCERTAIN")
        claims = [
            dict(text="原片记录：" + proof["claim"], support=support, evidence_ids=[eid])
            for eid, proof in proofs.items()
        ]
        answer.update(answer="所选候选的原片记录如下。", claims=claims)
        return _answer_step(answer)
    route = decision.get("plan", [])
    needs_visual = cp.attempt.intent == "INSPECT" or "INSPECT" in route
    visual_supported = False
    if needs_visual and request["query_mode"] == "LOCAL":
        answer = _visual_candidates(answer)
        if visual_identity(deps.planner):
            result = _search_evidence(deps, row, "inspect-candidates", request)
            checked = []
            for n, hit in distinct_candidates(result["hits"]):
                report = inspection(
                    row, f"inspect-{n}", hit["media_revision_id"], hit["range"],
                    request.get("text", "与查询画面一致"), generation=result["index_generation_id"],
                )
                if "evidence_id" in report:
                    checked.append(dict(
                        text=report["reason"], support=report["support"], evidence_ids=[report["evidence_id"]]
                    ))
            if checked:
                visual_supported = _supported(checked)
                answer = dict(
                    answer, claims=checked + answer["claims"], answer="；".join(c["text"] for c in checked),
                    warnings=[*answer["warnings"], "时序复核仅覆盖列明的候选片段。"],
                )
    if decision and request["query_mode"] == "LOCAL":
        if needs_visual and not visual_supported:
            return _answer_step(answer, "现有证据不足以确认所问画面。请选择候选回看，或提供同期台词和原片区间。")
        if not _supported(answer["claims"]):
            return _answer_step(answer, "目前只有候选，没有足够依据回答。请指定一个候选、原片区间或提供确切台词。")
    return _answer_step(answer)


def _intervals(hits, media_ids, *, first, source_order=False):
    if first or source_order:
        hits = sorted(hits, key=lambda hit: (media_ids.index(hit["media_revision_id"]), hit["range"]["start_us"]))
    if first:
        hits = hits[:1]
    return [(h["media_revision_id"], h["range"], h["evidence_ids"]) for h in hits]


def _dialogue_intervals(deps, row, query_id):
    cp, workspace = row.checkpoint, row.workspace_id
    plan, scope = cp.attempt.plan, cp.spec.scope
    request = dict(scope=scope.model_dump(mode="json"), text=f"{plan.get('text_source', '台词')}：{plan['query']}",
                   query_mode="ALL", top_k=12)
    answer = _search_evidence(deps, row, "dialogue", request, query_id=query_id)
    result_id = answer["complete_event_list_artifact_id"]
    files = deps.search.files
    if excluded := plan.get("must_exclude"):
        catalog = deps.search.catalog
        manifest = files.get(workspace, result_id, project=row.project_id)
        with files.path(manifest).open(encoding="utf8") as stream:
            scan = json.load(stream)
        generation = scan["index_generation_id"]
        _, rows = catalog.search_scope(workspace, row.project_id, scope.media_revision_ids, generation_id=generation)
        covered = catalog.literal_coverage(workspace, row.project_id, generation, scope.media_revision_ids)
        hits = exclude_subtitles(scan["events"], excluded, rows, covered)
    else:
        hits = files.occurrences(workspace, row.project_id, result_id)
    if plan.get("require_audio") and hits:
        audible = {
            mid for mid in {h["media_revision_id"] for h in hits}
            if any(s["kind"] == "audio" for s in deps.repo.sources.media(workspace, mid)["streams"])
        }
        hits = [h for h in hits if h["media_revision_id"] in audible]
        if not hits:
            raise NeedsInput("匹配的素材没有原音轨，无法保留讲话。请选择带声音的素材或调整音轨要求。", kind="CONSTRAINT_CONFLICT")
    intervals = _intervals(hits, scope.media_revision_ids, first=plan.get("first"))
    if not intervals:
        raise NeedsInput("没有找到可以核对的原片台词，请用引号输入原句或直接给出起止时间。")
    return intervals


def _verified_hits(deps, row, request):
    result = _search_evidence(deps, row, "search", request)
    brief = row.checkpoint.spec.brief.model_dump(mode="json")
    hits, snapshot = [], []
    # A first-pass subtitle or claim might not establish this user's visual brief.
    for checked, (n, hit) in enumerate(distinct_candidates(result["hits"], 5)):
        if checked >= 3 and hits:
            break
        observations = [
            observation for eid in hit["evidence_ids"]
            for observation in deps.repo.edits.resolve_evidence(row.workspace_id, eid)["observation_ids"]
        ]
        report = inspection(row, f"candidate-{n}", hit["media_revision_id"], hit["range"],
                            brief,
                            generation=result["index_generation_id"], underlying=observations)
        snapshot.append(candidate(row, n, hit, report))
        if report["support"] == "SUPPORTED":
            hits.append(dict(hit, verified=True, evidence_ids=[report["evidence_id"]]))
    if not hits:
        raise NeedsInput("尚未找到经过核实的对应画面或台词。可以改用明确的起止时间，或补充更具体的原片台词。")
    return hits, snapshot


def _referenced_interval(deps, row):
    attempt = row.checkpoint.attempt
    plan, selected = attempt.plan, attempt.selected_candidate
    original = None
    if plan["command"] == "candidate":
        if not selected or not selected["evidence_ids"]:
            raise NeedsInput("请先选择带原片依据的候选片段。")
        mid, span, ids = selected["media_revision_id"], selected["range"], selected["evidence_ids"]
    else:
        timeline = deps.repo.edits.get(
            revisions, row.workspace_id, attempt.base_id, lambda rev: rev.snapshot, edit_id=attempt.edit_id
        )
        clips = timeline["clips"]
        number = len(clips) if plan.get("last") else plan.get("clip_number", 1 if len(clips) == 1 else 0)
        if not 1 <= number <= len(clips):
            raise NeedsInput("请说明要修改时间线中的第几段，或指定最后一段。")
        original = clips[number - 1]
        mid, ids = original["media_revision_id"], original["evidence_ids"]
        span = dict(start_us=original["source_start_us"], end_us=original["source_end_us"])
    media, proofs = source_proofs(deps.repo, row, mid, span, ids)
    if selected and span["end_us"] - span["start_us"] <= 1:
        start, end = segment_bounds(span["start_us"], media["duration_us"])
        adjusted = any(k in plan for k in ("before_us", "after_us", "max_us"))
        span = dict(start_us=span["start_us"] if adjusted else start, end_us=end)
    window = relative_window(span, media["duration_us"], plan)
    retained = [eid for eid, p in proofs.items()
                if p["range"]["start_us"] < window["end_us"] and p["range"]["end_us"] > window["start_us"]]
    if proofs and not retained:
        raise NeedsInput("调整后的区间未包含选中的原片依据，请修改起点或时长。", kind="CONSTRAINT_CONFLICT")
    return [(mid, window, retained)], original


def _edit_material(deps, row, query_id):
    cp = row.checkpoint
    plan, scope = cp.attempt.plan, cp.spec.scope
    command, targets = plan["command"], cp.attempt.target_clip_ids
    original = snapshot = None
    base = base_revision(deps, row)
    if command == "quote":
        intervals = _dialogue_intervals(deps, row, query_id)
    elif command == "semantic":
        request = _query_for(scope.model_dump(mode="json"), plan["query"])
        if not visual_identity(deps.planner):
            answer = _search_evidence(deps, row, "edit-candidates", request, query_id=query_id)
            return _answer_step(dict(_visual_candidates(answer), run_id=row.id),
                                "当前无法核实要求的画面。可在候选中选定要剪的片段，或给出原片台词和区间。")
        hits, snapshot = _verified_hits(deps, row, request)
        intervals = _intervals(hits, scope.media_revision_ids, first=plan.get("first"),
                               source_order=cp.spec.brief.order_policy == "SOURCE_ORDER")
    elif command in ("candidate", "adjust_clip"):
        intervals, original = _referenced_interval(deps, row)
    elif command == "ranges":
        mid = scope.media_revision_ids[0]
        length = deps.repo.sources.media(row.workspace_id, mid)["duration_us"]
        if any(r["start_us"] >= r["end_us"] or r["end_us"] > length for r in plan["ranges"]):
            raise NeedsInput(f"选区超出原片或起止顺序有误，原片长 {length/1e6:g} 秒。")
        intervals = [(mid, span, []) for span in plan["ranges"]]
    else:
        intervals = []
    if command in ("quote", "semantic"):
        brief = local_brief(cp.spec.brief.model_dump(mode="json"), base, targets)
        if targets:
            intervals = select_replacement(brief, intervals, base, targets, alternative=plan.get("alternative", False))
        else:
            intervals = choose_intervals(brief, intervals)
        if len(intervals) > 200:
            raise NeedsInput("匹配超过一条时间线可容纳的 200 段，请缩小选区或指定首次出现。")
    if targets:
        original = next(c for c in base["clips"] if c["clip_id"] == targets[0])
        if len(intervals) != 1:
            raise NeedsInput("每轮局部修改只能替换目标片段一次，请指定一个区间。")
        if command == "ranges":
            brief = cp.spec.brief
            if brief.must_exclude:
                raise NeedsInput("精确区间仍须核查排除条件，请提供可核实的字幕条件。", kind="CONSTRAINT_CONFLICT")
            retained = {
                key for clip in base["clips"] if clip["clip_id"] not in targets for key in clip["evidence_ids"]
            }
            if brief.must_include:
                known = [
                    normalize_text(deps.repo.edits.resolve_evidence(row.workspace_id, key)["claim"])
                    for key in retained
                ]
                if any(not any(normalize_text(term) in item for item in known) for term in brief.must_include):
                    raise NeedsInput("无法确认替换后仍保留必要证据，请指定包含对应台词或画面证据的片段。", kind="CONSTRAINT_CONFLICT")
    proof = list(dict.fromkeys(e for _, _, ids in intervals for e in ids))
    docs = {}
    if snapshot is not None:
        proof.extend(e for c in snapshot for e in c["evidence_ids"] if e not in proof)
        docs["candidates"] = dict(index_generation_id=cp.spec.index_generation_id, candidates=snapshot)
    attempt = PlanAttempt(intervals=intervals, original_clip=original, candidate_snapshot=snapshot or [])
    return step("PROPOSE", attempt=attempt, docs=docs, pending_job_ids=[], evidence_ids=proof, event="EVIDENCE")
