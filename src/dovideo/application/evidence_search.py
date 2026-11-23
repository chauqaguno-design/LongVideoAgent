"""Find candidates in one authorized generation and ground literal claims in source evidence."""

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from functools import partial
from uuid import UUID, uuid4, uuid5

from fastapi import HTTPException
from PIL import Image, ImageChops

from dovideo.application.rank_fusion import RankedCandidate, reciprocal_rank_fusion
from dovideo.application.media_sampling import segment_bounds
from dovideo.application.retrieval_documents import normalize_text
from dovideo.application.sparse_retrieval import BM25Retriever
from dovideo.application.spec_contracts import validate_contract
from dovideo.infrastructure.search_artifacts import SearchArtifacts
from dovideo.infrastructure.search_cleanup import scoped_write
from dovideo.infrastructure.search_features import (
    PIXEL_SPACE,
    descriptor,
    frame_descriptors,
    read_query_image,
    similarity,
)
from dovideo.infrastructure.vector.qdrant import QdrantVectorError


def time_range(start, end):
    return {"start_us": start, "end_us": end}


def overlap(left, right):
    return left["start_us"] < right["end_us"] and right["start_us"] < left["end_us"]


def playback_range(row, bounds, duration=None):
    span = row["range"]
    if row.get("frame") and span["end_us"] - span["start_us"] <= 1:
        # Frame evidence remains a point. Its segment is the 10 s window on the 8 s source grid.
        start, end = segment_bounds(row["frame"]["actual_us"], duration or bounds["end_us"])
        return time_range(max(bounds["start_us"], start), min(bounds["end_us"], end))
    return time_range(max(bounds["start_us"], span["start_us"]), min(bounds["end_us"], span["end_us"]))


def split_coverage(requested, checked):
    """Clip and union checked ranges, then subtract them from each requested interval."""
    verified, gaps = [], []
    for item in requested:
        mid, bounds = item["media_revision_id"], item["range"]
        cursor, stop = bounds["start_us"], bounds["end_us"]
        merged = []
        for r in sorted(checked.get(mid, []), key=lambda x: x["start_us"]):
            start, end = max(cursor, r["start_us"]), min(stop, r["end_us"])
            if start >= end:
                continue
            if merged and start <= merged[-1]["end_us"]:
                merged[-1]["end_us"] = max(end, merged[-1]["end_us"])
            else:
                merged.append(time_range(start, end))
        for r in merged:
            if cursor < r["start_us"]:
                gaps.append(dict(media_revision_id=mid, range=time_range(cursor, r["start_us"])))
            verified.append(dict(media_revision_id=mid, range=r))
            cursor = r["end_us"]
        if cursor < stop:
            gaps.append(dict(media_revision_id=mid, range=time_range(cursor, stop)))
    return verified, gaps


def coverage(requested, *, run_id=None, protocol=None, checked=None):
    verified, gaps = split_coverage(requested, checked or {})
    status = "UNASSESSED" if not run_id else "PARTIAL"
    if run_id and requested and not gaps:
        status = "PROTOCOL_COMPLETE"
    return dict(
        status=status,
        query_run_id=run_id,
        protocol_id=protocol,
        requested_ranges=requested,
        verified_ranges=verified,
        unverified_ranges=gaps,
        semantic_exhaustiveness_guaranteed=False,
    )


def query_literal(text):
    """Only an explicit quote/subtitle instruction defines an exhaustive text-search protocol."""
    match = re.search(r'["“「『](.+?)["”」』]', text)
    if match:
        return match.group(1).strip()
    match = re.match(r"^(?:字幕|台词|对白|subtitle|quote)\s*[:：]\s*(.+)$", text, re.I)
    return match.group(1).strip() if match else None


@dataclass
class QueryPlan:
    project: str
    generation: str
    requested: list
    rows: list
    fingerprint: str
    durations: dict | None = None


class EvidenceSearch:
    def __init__(self, sources, catalog, encoder=None):
        self.sources, self.catalog, self.encoder = sources, catalog, encoder
        self.files = SearchArtifacts(sources)
        if encoder:
            encoder.write_guard = partial(scoped_write, sources)

    def prepare_rows(self, workspace, rows):
        if self.encoder:
            try:
                return self.encoder.prepare(workspace, rows)
            except (OSError, ValueError, KeyError, QdrantVectorError) as exc:
                logging.getLogger(__name__).warning(
                    "Embedding projection unavailable (%s)", type(exc).__name__
                )
        return {}

    def verified_evidence(self, workspace, evidence_id):
        return self.files.verified(workspace, evidence_id)

    def close(self):
        if self.encoder and self.encoder.index:
            self.encoder.index.close()

    def verify_picture(self, workspace, run_id, image_id, plan, result):
        _, query = read_query_image(self.files.image(workspace, plan.project, image_id))
        roi = self.files.roi(workspace, plan.project, image_id)
        candidates = {row["evidence_id"]: row for row in plan.rows}
        inspected = set()
        matches = []
        for hit in result["hits"][:8]:
            for eid in hit["evidence_ids"]:
                row = candidates[eid]
                frame = row.get("frame")
                if not frame or not row.get("path") or frame["artifact_id"] in inspected:
                    continue
                if len(inspected) >= 8:
                    return matches
                inspected.add(frame["artifact_id"])
                try:
                    with Image.open(row["path"]) as src:
                        pixels = src.convert("RGB")
                except OSError:
                    continue
                if roi:
                    if list(pixels.size) != roi["original_size"]:
                        continue
                    box = roi["roi"]
                    x, y, w, h = (box[k] for k in ("x", "y", "width", "height"))
                    pixels = pixels.crop((x, y, x + w, y + h))
                equal = pixels.size == query.size and ImageChops.difference(pixels, query).getbbox() is None
                if not equal:
                    continue
                actual = frame["actual_us"]
                verified_id = str(uuid5(UUID(run_id), eid + ":exact-rgb"))
                proof = dict(
                    evidence_id=verified_id,
                    workspace_id=workspace,
                    project_id=plan.project,
                    media_revision_id=row["media_revision_id"],
                    range=time_range(actual, actual + 1),
                    claim=f"{actual / 1e6:.3f} 秒处的源帧{'选区' if roi else ''}与查询截图的 RGB 像素相同",
                    support="SUPPORTED",
                    observation_ids=row["observation_ids"],
                    artifact_ids=[image_id, frame["artifact_id"]]
                    + ([roi["parent_artifact_id"]] if roi else []),
                    verification_protocol="exact-rgb-v1/" + run_id,
                    index_generation_id=None,
                )
                payload = dict(
                    run_id=run_id,
                    source_generation=plan.generation,
                    source_evidence_id=eid,
                    evidence=proof,
                )
                self.files.save(
                    workspace,
                    plan.project,
                    json.dumps(payload, ensure_ascii=False).encode(),
                    "QUERY_EVIDENCE",
                    "application/json",
                    [row["media_revision_id"]],
                    ident=verified_id,
                )
                matches.append(proof)
        return matches

    def plan(self, workspace, request):
        try:
            validate_contract("SearchRequest", request)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        project = request["scope"]["project_id"]
        self.sources.project(workspace, project)
        if not (request.get("text") or "").strip() and not request.get("image_artifact_id"):
            raise HTTPException(422, "请提供文字或查询图片")
        chosen = list(dict.fromkeys(request["scope"].get("media_revision_ids", [])))
        if chosen:
            durations = {}
            for mid in chosen:
                media = self.sources.media(workspace, mid)
                if self.sources.asset(workspace, media["asset_id"])["project_id"] != project:
                    raise HTTPException(404, "素材不存在")
                durations[mid] = media["duration_us"]
        else:
            durations, cursor = {}, None
            while True:
                page = self.sources.page(workspace, cursor, 100, project)
                durations.update(
                    (m["media_revision_id"], m["duration_us"])
                    for m in page["items"]
                    if m["status"] == "AVAILABLE"
                )
                cursor = page["next_cursor"]
                if not cursor:
                    break
        if request["query_mode"] == "FIRST" and len(durations) > 1:
            raise HTTPException(422, "请限定一部视频后再找首次出现的位置")
        bounds = request.get("range")
        if bounds and (
            bounds["end_us"] <= bounds["start_us"] or any(bounds["end_us"] > d for d in durations.values())
        ):
            raise HTTPException(422, "查询区间为空或超出原片时长")
        ranges = [
            dict(media_revision_id=mid, range=bounds or time_range(0, duration))
            for mid, duration in durations.items()
        ]
        generation, snapshot = self.catalog.search_scope(workspace, project, list(durations))
        # Scope the inputs before scoring, and pin one generation for every retrieval route.
        rows = self.scoped_rows(workspace, project, ranges, snapshot)
        signature = [
            workspace,
            project,
            generation,
            ranges,
            request.get("text"),
            request.get("image_artifact_id"),
            request["query_mode"],
            "rrf60-v1",
        ]
        fp = hashlib.sha256(json.dumps(signature, sort_keys=True).encode()).hexdigest()
        return QueryPlan(project, generation, ranges, rows, fp, durations)

    @staticmethod
    def scoped_rows(workspace, project, ranges, snapshot):
        bounds = {r["media_revision_id"]: r["range"] for r in ranges}
        rows = [
            r
            for r in snapshot
            if r["workspace_id"] == workspace
            and r["project_id"] == project
            and r["media_revision_id"] in bounds
            and overlap(bounds[r["media_revision_id"]], r["range"])
        ]
        return sorted(
            rows,
            key=lambda r: (
                r["media_revision_id"],
                r["range"]["start_us"],
                r["evidence_id"],
            ),
        )

    def rank(self, workspace, request, plan):
        text = (request.get("text") or "").strip()
        rows = plan.rows
        warnings, routes = [], {}
        if text:
            query = query_literal(text) or text
            documents = [r["claim"] if r.get("source") != "sampled-frame" else "" for r in rows]
            routes["bm25"] = (
                "zh-en-bm25-v1",
                BM25Retriever().rank(query, documents, limit=len(rows)),
            )
            needle = normalize_text(query)
            if needle:
                exact = [
                    RankedCandidate(i, len(needle) / max(len(needle), len(normalize_text(doc))))
                    for i, doc in enumerate(documents)
                    if needle in normalize_text(doc)
                ]
                routes["verbatim"] = (
                    "literal-v1",
                    sorted(exact, key=lambda c: (-c.score, c.index)),
                )
        image_id = request.get("image_artifact_id")
        image = None
        if image_id:
            _, image = read_query_image(self.files.image(workspace, plan.project, image_id))
            query_pixels = descriptor(image)
            scored = []
            for i, row in enumerate(rows):
                frame = row.get("frame")
                if frame and row.get("path"):
                    try:
                        features = frame_descriptors(str(row["path"]), frame["sha256"])
                    except OSError:
                        warnings.append("部分已索引帧的文件暂时不可读")
                        continue
                    scored.append(RankedCandidate(i, similarity(query_pixels, features)))
            if not scored:
                warnings.append("所选范围尚无可用的已解码索引帧")
            routes["image_spatial"] = (
                PIXEL_SPACE,
                sorted(scored, key=lambda c: (-c.score, c.index)),
            )
        if self.encoder:
            self.encoder.rank(text, image, rows, routes, warnings)
        elif text:
            warnings.append("语义编码器未配置，本次文字召回使用词法索引")
        # One source occurrence may be present in several overlapping segments.
        representatives, by_source = {}, {}
        for i, row in enumerate(rows):
            key = (
                row["media_revision_id"],
                row["range"]["start_us"],
                row["range"]["end_us"],
                normalize_text(row["claim"]),
            )
            representatives[i] = by_source.setdefault(key, i)
        sources, trace, lists = {}, {}, []
        for name, (model, ranking) in routes.items():
            seen, distinct = set(), []
            for c in ranking:
                i = representatives[c.index]
                sources.setdefault(i, set()).add(rows[c.index]["evidence_id"])
                if i in seen:
                    continue
                seen.add(i)
                distinct.append(RankedCandidate(i, c.score))
                trace.setdefault(i, []).append(
                    dict(
                        route=name,
                        rank=len(distinct),
                        raw_score=c.score,
                        model_version=model,
                    )
                )
            lists.append(distinct)
        fused = reciprocal_rank_fusion(lists, limit=len(rows))
        bounds = {item["media_revision_id"]: item["range"] for item in plan.requested}
        hits = []
        for c in fused[: request["top_k"]]:
            r = rows[c.index]
            hits.append(
                dict(
                    segment_id=r.get("segment_id") or r["evidence_id"],
                    media_revision_id=r["media_revision_id"],
                    range=playback_range(
                        r,
                        bounds[r["media_revision_id"]],
                        plan.durations[r["media_revision_id"]],
                    ),
                    evidence_ids=sorted(sources[c.index]),
                    score=c.score,
                    route_ranks=trace[c.index],
                    verified=False,
                )
            )
        return dict(
            query_id=str(uuid4()),
            index_generation_id=plan.generation,
            hits=hits,
            warnings=list(dict.fromkeys(warnings)),
            coverage_summary=coverage(plan.requested),
            candidate_count=len(fused),
            truncated=len(fused) > len(hits),
            verification_required=request["query_mode"] != "LOCAL",
            verification_workflow_id=None,
            result_artifact_id=None,
        )

    def search(self, workspace, request):
        plan = self.plan(workspace, request)
        result = self.rank(workspace, request, plan)
        self.recheck_scope(workspace, plan)
        return result

    def recheck_scope(self, workspace, plan):
        self.sources.project(workspace, plan.project)
        for item in plan.requested:
            self.sources.media(workspace, item["media_revision_id"])

    def answer(self, workspace, run_id, request):
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        frozen = self.files.scan(workspace, run_id, request_hash)
        if frozen and frozen.answer:
            return frozen.answer
        plan = None
        if not frozen:
            plan = self.plan(workspace, request)
            result = self.rank(workspace, request, plan)
            checked = self.catalog.literal_coverage(
                workspace,
                plan.project,
                plan.generation,
                [r["media_revision_id"] for r in plan.requested],
            )
            frozen = self.files.freeze(
                workspace,
                run_id,
                request_hash,
                dict(
                    project=plan.project,
                    generation=plan.generation,
                    requested=plan.requested,
                    fingerprint=plan.fingerprint,
                    checked=checked,
                    result=result,
                ),
            )
        snapshot = frozen.snapshot
        if plan is None or plan.fingerprint != snapshot["fingerprint"]:
            media = [r["media_revision_id"] for r in snapshot["requested"]]
            generation, records = self.catalog.search_scope(
                workspace,
                snapshot["project"],
                media,
                generation_id=snapshot["generation"],
            )
            rows = self.scoped_rows(workspace, snapshot["project"], snapshot["requested"], records)
            plan = QueryPlan(
                snapshot["project"],
                generation,
                snapshot["requested"],
                rows,
                snapshot["fingerprint"],
            )
        answer = self.ground(workspace, run_id, request, plan, snapshot["result"], snapshot["checked"])
        self.recheck_scope(workspace, plan)
        self.files.finish(workspace, run_id, answer)
        return answer

    def ground(self, workspace, run_id, request, plan, result, checked):
        mode = request["query_mode"]
        text = request.get("text") or ""
        literal = query_literal(text)
        subtitle_protocol = (
            literal
            and not request.get("image_artifact_id")
            and ("字幕" in text or "subtitle" in text.lower())
        )
        protocol = "embedded-subtitle-literal-v1" if subtitle_protocol else "candidate-review-v1"
        if subtitle_protocol:
            # An unresolved subtitle observation leaves a gap in this particular literal scan.
            conflicts = {}
            for row in plan.rows:
                if (
                    row.get("source") == "embedded-subtitle"
                    and row["support"] != "SUPPORTED"
                    and normalize_text(literal) in normalize_text(row["claim"])
                ):
                    conflicts.setdefault(row["media_revision_id"], []).append(row["range"])
            known = [dict(media_revision_id=mid, range=r) for mid, spans in checked.items() for r in spans]
            _, clear = split_coverage(known, conflicts)
            checked = {}
            for item in clear:
                checked.setdefault(item["media_revision_id"], []).append(item["range"])
        summary = coverage(
            plan.requested,
            run_id=run_id,
            protocol=protocol,
            checked=checked if subtitle_protocol else {},
        )
        selected = {e for h in result["hits"] for e in h["evidence_ids"]}
        claims, events = [], []
        build = {}
        for row in plan.rows:
            # A caption directly supports text it contains; a frame candidate alone cannot prove an action.
            source = row.get("source")
            exact = bool(literal and normalize_text(literal) in normalize_text(row["claim"]))
            supported = (
                exact
                and row["support"] == "SUPPORTED"
                and (source == "embedded-subtitle" or (not subtitle_protocol and source == "faster-whisper"))
            )
            if supported:
                key = (row["media_revision_id"], normalize_text(row["claim"]))
                previous = build.get(key)
                if previous and overlap(previous["range"], row["range"]):
                    previous["range"]["end_us"] = max(previous["range"]["end_us"], row["range"]["end_us"])
                    previous["evidence_ids"].append(row["evidence_id"])
                else:
                    event = dict(
                        occurrence_id=str(uuid5(UUID(run_id), row["evidence_id"])),
                        status="SUPPORTED",
                        media_revision_id=row["media_revision_id"],
                        range=dict(row["range"]),
                        evidence_ids=[row["evidence_id"]],
                        text=row["claim"],
                        source=source,
                    )
                    events.append(event)
                    build[key] = event
            if row["evidence_id"] in selected:
                support = (
                    "SUPPORTED"
                    if supported
                    else "CONFLICTING"
                    if row["support"] == "CONFLICTING"
                    else "UNCERTAIN"
                )
                claims.append(
                    dict(
                        text=row["claim"],
                        evidence_ids=[row["evidence_id"]],
                        support=support,
                    )
                )
        # Once a quotation is grounded, show its evidence and competing readings.
        # Other nearby dialogue and generic frame matches belong in search results.
        if events and not request.get("image_artifact_id"):
            claims = [
                claim for claim in claims
                if claim["support"] != "UNCERTAIN" or normalize_text(literal) in normalize_text(claim["text"])
            ]
        complete = summary["status"] == "PROTOCOL_COMPLETE"
        warnings = list(result["warnings"])
        if not complete:
            warnings.append("未核查的源时间区间列于覆盖记录中")
        count, qualifier, artifact_id = None, None, None
        if mode != "LOCAL":
            count, qualifier = (
                len(events),
                "WITHIN_PROTOCOL" if complete else "OBSERVED_ONLY",
            )
            payload = dict(
                query_fingerprint=plan.fingerprint,
                index_generation_id=plan.generation,
                scope=plan.requested,
                query=text,
                protocol_id=protocol,
                coverage_summary=summary,
                count_qualifier=qualifier,
                instance_definition="每处含目标原文的独立源字幕或转写区间",
                events=events,
            )
            artifact_id = self.files.save(
                workspace,
                plan.project,
                json.dumps(payload, ensure_ascii=False).encode(),
                "QUERY_RESULT",
                "application/json",
                [r["media_revision_id"] for r in plan.requested],
                ident=str(uuid5(UUID(run_id), "complete-occurrences")),
            )
        if mode == "FIRST" and events:
            first = min(events, key=lambda e: e["range"]["start_us"])
            first_complete = complete
            if subtitle_protocol:
                # FIRST requires the prefix through this occurrence; suffix gaps still limit the count.
                prefix = [
                    dict(
                        media_revision_id=r["media_revision_id"],
                        range=time_range(
                            r["range"]["start_us"],
                            min(r["range"]["end_us"], first["range"]["end_us"]),
                        ),
                    )
                    for r in plan.requested
                    if r["range"]["start_us"] < first["range"]["end_us"]
                ]
                prefix_summary = coverage(prefix, run_id=run_id, protocol=protocol, checked=checked)
                first_complete = prefix_summary["status"] == "PROTOCOL_COMPLETE"
            claims.insert(
                0,
                dict(
                    text=first["text"],
                    evidence_ids=first["evidence_ids"],
                    support="SUPPORTED",
                ),
            )
            lead = "按内嵌字幕逐条核查，首次匹配" if first_complete else "目前证据中最早的文字匹配"
            answer = f"{lead}在 {first['range']['start_us'] / 1e6:.3f} 秒。"
        elif mode != "LOCAL" and not literal:
            answer = "现有证据尚不足以确认该事件的出现次数，已核实的下界为 0。"
        elif mode != "LOCAL":
            lead = "已核查所选区间的内嵌字幕" if complete else "目前可核对的源文字"
            answer = f"{lead}，发现 {len(events)} 处匹配。"
        elif events:
            event = next(
                (e for e in events if selected.intersection(e["evidence_ids"])),
                events[0],
            )
            claims.insert(
                0,
                dict(
                    text=event["text"],
                    evidence_ids=event["evidence_ids"],
                    support="SUPPORTED",
                ),
            )
            answer = f"在 {event['range']['start_us'] / 1e6:.3f} 秒处找到原文「{literal}」。"
        elif claims:
            answer = "找到了可回看的候选片段，现有证据尚不足以确认所问事件。"
        else:
            answer = "在已建立索引的素材中没有找到相关证据。"
        if mode == "LOCAL" and request.get("image_artifact_id"):
            proofs = self.verify_picture(workspace, run_id, request["image_artifact_id"], plan, result)
            if proofs:
                answer = proofs[0]["claim"] + "。"
                claims = [
                    dict(
                        text=e["claim"],
                        evidence_ids=[e["evidence_id"]],
                        support="SUPPORTED",
                    )
                    for e in proofs
                ]
                checked = {}
                for e in proofs:
                    checked.setdefault(e["media_revision_id"], []).append(e["range"])
                summary = coverage(
                    plan.requested,
                    run_id=run_id,
                    protocol="exact-rgb-v1",
                    checked=checked,
                )
        claims = list({tuple(c["evidence_ids"]): c for c in claims}.values())
        return dict(
            run_id=run_id,
            answer=answer,
            claims=claims[: request["top_k"]],
            coverage_summary=summary,
            found_event_count=count,
            count_qualifier=qualifier,
            complete_event_list_artifact_id=artifact_id,
            search_query_ids=[result["query_id"]],
            warnings=warnings,
        )
