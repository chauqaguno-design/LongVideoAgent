"""Frozen VideoMind quality metrics for independent annotations and saved predictions."""

from collections import Counter, defaultdict
from itertools import islice
from math import ceil
import re
from statistics import mean
from .release_audits import control_gates, status

US = 1_000_000
THRESHOLDS = {
    "text_recall_at_20": 0.90,
    "image_recall_at_20": 0.85,
    "grounding_r5_iou50": 0.80,
    "claim_precision": 0.95,
    "boundary_start_p95_us": 3 * US,
    "boundary_end_p95_us": 3 * US,
    "unqualified_global_claims": 0,
    "tenant_leaks": 0,
    "out_of_scope_results": 0,
}
GROUPS = ("position", "language", "modality", "motion", "event_duration", "asr_quality", "near_negative")


def require(ok, reason):
    if not ok:
        raise ValueError(reason)


def validate_manifest(dataset, *, dev=None):
    """Blind sets must pin independent source content, labels, reviewers and declared coverage."""
    require(dataset["split"] in {"DEV", "BLIND"}, "unknown split")
    media = {m["id"]: m for m in dataset["media"]}
    require(len(media) == len(dataset["media"]) and bool(media), "missing or repeated media")
    digests, origins = set(), set()
    for item in media.values():
        require(
            re.fullmatch("[0-9a-f]{64}", item["sha256"]) and item["license"] and item["origin_id"],
            "media must have an immutable digest, license and source family",
        )
        require(item["duration_us"] > 0 and item["category"], "invalid media metadata")
        require(item["sha256"] not in digests, "duplicate content under a second media id")
        digests.add(item["sha256"])
        origins.add(item["origin_id"])
    queries = dataset["queries"]
    require(len({q["id"] for q in queries}) == len(queries) and bool(queries), "empty or duplicated queries")
    for q in queries:
        require(q["type"] in {"TEXT", "IMAGE", "FIRST", "ALL", "COUNT"}, "unknown query type")
        require(q["scope"] and set(q["scope"]) <= media.keys(), "invalid query scope")
        require(
            q["query"]
            and q["event_definition"]
            and q["annotator"]
            and q["reviewer"]
            and q["reviewer"] != q["annotator"],
            "independent annotation/review required",
        )
        require(set(GROUPS) <= q["groups"].keys(), "missing subgroup label")
        if q["type"] in {"FIRST", "ALL", "COUNT"}:
            require(
                q["exhaustive"] is True and q.get("dedup_rule"),
                "global query requires exhaustive adjudication",
            )
        truth = {}
        for e in q["events"]:
            require(e["media"] in q["scope"] and e["basis"] and e["id"], "unscoped or unsupported label")
            require(0 <= e["start_us"] <= e["end_us"] <= media[e["media"]]["duration_us"], "invalid GT time")
            require(e.get("boundary") in {"CLEAR", "AMBIGUOUS", "FRAME"}, "invalid boundary tag")
            require(e["id"] not in truth or truth[e["id"]] == e, "duplicate GT id with different meaning")
            truth[e["id"]] = e
            for alt in e.get("alternatives", []):
                require(
                    alt["media"] in q["scope"]
                    and alt.get("basis")
                    and 0 <= alt["start_us"] <= alt["end_us"] <= media[alt["media"]]["duration_us"],
                    "invalid allowed alternative",
                )
        negatives = set(q.get("negative_sources", []))
        positive_sources = {e["media"] for gt in truth.values() for e in (gt, *gt.get("alternatives", []))}
        require(negatives <= set(q["scope"]) and not negatives & positive_sources, "invalid negative source")
        if q["type"] in {"TEXT", "IMAGE"}:
            require(len(truth) <= 20, "more than 20 independent events belong in a global query")
    if dataset["split"] == "BLIND":
        require(dev is not None and dev["split"] == "DEV", "independent development manifest required")
        require(
            dataset.get("annotation_protocol") == "independent-human-review" and dataset.get("frozen_at"),
            "freeze and independently review blind labels before prediction",
        )
        require(all(m.get("rights_ref") for m in media.values()), "missing blind media rights reference")
        validate_manifest(dev)
        require(
            not (
                digests & {m["sha256"] for m in dev["media"]}
                or origins & {m["origin_id"] for m in dev["media"]}
            ),
            "development and blind sets share source content",
        )
        require(not ({q["id"] for q in queries} & {q["id"] for q in dev["queries"]}), "reused query IDs")
        for q in queries:
            require(
                isinstance(q.get("allowed_answers"), list)
                and type(q.get("answerable")) is bool
                and type(q.get("exhaustive")) is bool,
                "missing frozen acceptable answers or answerability",
            )
            if q["type"] in {"ALL", "COUNT"}:
                require(q.get("exclusion_rule"), "global query requires frozen exclusions")
        durations = [m["duration_us"] for m in media.values()]
        hour = 3600 * US
        require(
            len(media) == 24
            and sum(durations) >= 12 * hour
            and sum(d >= hour for d in durations) >= 6
            and sum(d >= 2 * hour for d in durations) >= 2,
            "blind footage composition below frozen target",
        )
        require(
            {"lecture_meeting", "story", "sports_action", "screencast"}
            <= {m["category"] for m in media.values()},
            "blind collection lacks required content groups",
        )
        require(
            Counter(q["type"] for q in queries)
            == {"TEXT": 200, "IMAGE": 60, "FIRST": 20, "ALL": 20, "COUNT": 20},
            "blind query composition differs from frozen target",
        )


def matched_span(gt, hit, kind):
    if gt["media"] != hit["media"]:
        return False
    a, b = gt["start_us"], gt["end_us"]
    if kind == "IMAGE":
        t = hit.get("actual_us")
        return t is not None and (abs(t - a) <= 2 * US if a == b else a <= t < b)
    c, d = hit["start_us"], hit["end_us"]
    if d <= c or b <= a:
        return False
    intersection = max(0, min(b, d) - max(a, c))
    if kind == "TEXT":
        return d - c <= 20 * US and intersection / min(b - a, d - c) >= 0.5
    return intersection / (max(b, d) - min(a, c)) >= 0.5


def matched(gt, hit, kind):
    return any(matched_span(span, hit, kind) for span in (gt, *gt.get("alternatives", [])))


def unique_candidates(candidates, kind):
    fields = ("media", "actual_us") if kind == "IMAGE" else ("media", "start_us", "end_us")
    seen = set()
    for c in candidates:
        key = tuple(c.get(f) for f in fields)
        if key not in seen:
            seen.add(key)
            yield c


def assessed_coverage(q, p, media):
    """Measure verified and failed source time within the frozen query range or necessary FIRST prefix."""
    review = p.get("coverage_review")
    if not review:
        return None
    require(review["reviewer"] and review["basis"], "missing independent coverage review")
    requested = q.get(
        "ranges", [dict(media=mid, start_us=0, end_us=media[mid]["duration_us"]) for mid in q["scope"]]
    )

    def group(spans):
        by_media = defaultdict(list)
        for span in spans:
            mid, a, b = span["media"], span["start_us"], span["end_us"]
            require(mid in q["scope"] and 0 <= a < b <= media[mid]["duration_us"], "invalid coverage bounds")
            by_media[mid].append((a, b))
        merged = {}
        for mid, intervals in by_media.items():
            merged[mid] = []
            for a, b in sorted(intervals):
                if merged[mid] and a <= merged[mid][-1][1]:
                    a0, b0 = merged[mid][-1]
                    merged[mid][-1] = (a0, max(b, b0))
                else:
                    merged[mid].append((a, b))
        return merged

    source = group(requested)
    if q["type"] == "FIRST" and p["grounded"]:
        require(len(q["scope"]) == 1, "FIRST lacks a single ordered source")
        stop = p["grounded"][0]["end_us"]
        source = {mid: [(a, min(b, stop)) for a, b in spans if a < stop] for mid, spans in source.items()}

    def intersection(left, right):
        return sum(
            max(0, min(b, d) - max(a, c))
            for mid, spans in left.items()
            for a, b in spans
            for c, d in right.get(mid, [])
        )

    verified, failed = group(review["verified"]), group(review["failed"])
    require(intersection(verified, failed) == 0, "time cannot be both verified and failed")
    total = sum(b - a for spans in source.values() for a, b in spans)
    checked = intersection(source, verified)
    return dict(
        requested_us=total,
        verified_us=checked,
        failed_us=intersection(source, failed),
        complete=total > 0 and checked == total,
    )


def disjoint_matches(truth, predictions):
    """Maximum cardinality matching; a predicted occurrence can support only one GT."""
    partner = {}

    def assign(i, seen):
        for j, pred in enumerate(predictions):
            if j in seen or not matched(truth[i], pred, "IOU"):
                continue
            seen.add(j)
            if j not in partner or assign(partner[j], seen):
                partner[j] = i
                return True
        return False

    for i in range(len(truth)):
        assign(i, set())
    return [(truth[i], predictions[j]) for j, i in partner.items()]


def percentile(values, p):
    return sorted(values)[ceil(len(values) * p) - 1] if values else None


def score_query(q, prediction, media, blind):
    truth = list({e["id"]: e for e in q["events"]}.values())
    kind = q["type"]
    claims = prediction["claims"]
    review = prediction.get("answer_review", {})
    execution = prediction.get("execution_status", None if blind else "SUCCEEDED")
    require(
        execution in {"SUCCEEDED", "PARTIAL", "FAILED", "CANCELLED", "TIMED_OUT"},
        "missing or invalid query execution status",
    )
    failed = execution not in {"SUCCEEDED", "PARTIAL"}
    if blind:
        require(
            review.get("source") == "human"
            and review.get("reviewer")
            and review.get("basis")
            and type(review.get("factual_claim_count")) is int
            and review["factual_claim_count"] == len(claims)
            and type(review.get("confirms_match")) is bool
            and type(review.get("claims_exhaustive")) is bool,
            "blind answers require independent whole-answer review",
        )
    for c in claims:
        require(isinstance(c["supported"], bool) and c["reviewer"], "claim lacks independent review")
        if blind:
            require(
                c.get("text") and c.get("basis") and (not c["supported"] or c.get("evidence_ids")),
                "supported claims need evidence and independent adjudication",
            )
    require(type(prediction["answered"]) is bool, "answered must be boolean")
    row = dict(
        id=q["id"],
        type=kind,
        truth_count=len(truth),
        claim_count=len(claims),
        supported_claims=sum(c["supported"] for c in claims) if prediction["answered"] and not failed else 0,
        answered=bool(prediction["answered"] and not failed),
        execution_failed=failed,
        answerable=q.get("answerable", bool(truth)),
        tenant_leaks=prediction["tenant_leaks"],
        unqualified_global_claims=0,
        start_errors=[],
        end_errors=[],
    )
    require(type(row["tenant_leaks"]) is int and row["tenant_leaks"] >= 0, "invalid tenant audit count")
    candidates, grounded = prediction["candidates"], prediction["grounded"]
    row["out_of_scope_results"] = sum(s["media"] not in q["scope"] for s in [*candidates, *grounded])
    if failed:
        candidates, grounded = [], []
    if kind in {"TEXT", "IMAGE"}:
        distinct = list(islice(unique_candidates(candidates, kind), 20))
        if truth:
            row["recall"] = mean(any(matched(e, c, kind) for c in distinct) for e in truth)
            intervals = [e for e in truth if e["start_us"] < e["end_us"]]
            row["grounded"] = False
            row["point_only_positive"] = not intervals
            if intervals:
                pairs = disjoint_matches(intervals, grounded[:5])
                row["grounded"] = bool(pairs)
                eligible = [
                    e for e in intervals if e["boundary"] == "CLEAR" and e["end_us"] - e["start_us"] >= 2 * US
                ]
                row["clear_event_count"] = len(eligible)
                row["short_event_count"] = sum(e["end_us"] - e["start_us"] < 2 * US for e in intervals)
                row["unmatched_events"] = len(intervals) - len(pairs)
                row["unmatched_predictions"] = len(grounded[:5]) - len(pairs)
                row["ambiguous_events"] = sum(e["boundary"] == "AMBIGUOUS" for e in intervals)
                for gt, pred in pairs:
                    if gt in eligible:
                        spans = [s for s in (gt, *gt.get("alternatives", [])) if matched_span(s, pred, "IOU")]
                        accepted = min(
                            spans,
                            key=lambda s: abs(s["start_us"] - pred["start_us"])
                            + abs(s["end_us"] - pred["end_us"]),
                        )
                        row["start_errors"].append(abs(accepted["start_us"] - pred["start_us"]))
                        row["end_errors"].append(abs(accepted["end_us"] - pred["end_us"]))
        else:
            row["negative_has_candidate"] = bool(candidates)
            row["negative_false_confirmation"] = bool(grounded) or bool(review.get("confirms_match"))
        if q.get("negative_sources"):
            row["wrong_source_candidate"] = any(h["media"] in q["negative_sources"] for h in distinct)
            row["wrong_source_confirmation"] = any(h["media"] in q["negative_sources"] for h in grounded)
    else:
        covered = assessed_coverage(q, prediction, media)
        row["coverage_audited"] = covered is not None
        row["protocol_complete"] = prediction["coverage"] == "PROTOCOL_COMPLETE" and bool(
            covered and covered["complete"]
        )
        if covered and failed:
            covered = dict(covered, complete=False, verified_us=0, failed_us=covered["requested_us"])
        if covered:
            row.update({k: v for k, v in covered.items() if k.endswith("_us")})
        if failed:
            row["protocol_complete"] = False
        require(type(prediction["count"]) is int and prediction["count"] >= 0, "invalid count")
        claims_all = prediction["claims_exhaustive"] or review.get("claims_exhaustive")
        known_incomplete = (
            not covered["complete"] if covered else prediction["coverage"] != "PROTOCOL_COMPLETE"
        )
        row["unqualified_global_claims"] = int(bool(claims_all) and (failed or known_incomplete))
        pairs = disjoint_matches(truth, grounded)
        row.update(
            matches=len(pairs),
            predictions=len(grounded),
            count_error=abs(prediction["count"] - len(truth)),
            lower_bound_valid=prediction["count"] <= len(pairs) and len(pairs) == len(grounded),
        )
        if kind == "FIRST":
            first = min(truth, key=lambda e: e["start_us"]) if truth else None
            row["first_correct"] = (
                matched(first, grounded[0], "IOU")
                if grounded and first
                else not grounded and not first and row["answered"] and row["protocol_complete"]
            )
    return row


def aggregate(rows):
    def avg(key, kind=None):
        vals = [r[key] for r in rows if key in r and (not kind or r["type"] == kind)]
        return mean(vals) if vals else None

    claims = sum(r["claim_count"] for r in rows)
    count = [r["count_error"] for r in rows if r["type"] == "COUNT"]
    allrows = [r for r in rows if r["type"] == "ALL"]
    found = sum(r["predictions"] for r in allrows)
    truth = sum(r["truth_count"] for r in allrows)
    pairs = sum(r["matches"] for r in allrows)
    answerable = [r for r in rows if r["answerable"]]
    requested = sum(r.get("requested_us", 0) for r in rows)
    return dict(
        queries=len(rows),
        text_recall_at_20=avg("recall", "TEXT"),
        image_recall_at_20=avg("recall", "IMAGE"),
        grounding_r5_iou50=avg("grounded"),
        claim_precision=sum(r["supported_claims"] for r in rows) / claims if claims else None,
        answered_rate=avg("answered"),
        failed_queries=sum(r["execution_failed"] for r in rows),
        execution_failure_rate=avg("execution_failed"),
        answerable_rate=avg("answerable"),
        answer_coverage=mean(r["answered"] for r in answerable) if answerable else None,
        supported_answer_coverage=mean(
            bool(r["answered"] and r["claim_count"] and r["claim_count"] == r["supported_claims"])
            for r in answerable
        )
        if answerable
        else None,
        abstention_rate=1 - avg("answered") if rows else None,
        first_correct_rate=avg("first_correct"),
        all_precision=pairs / found if found else None,
        all_recall=pairs / truth if truth else None,
        count_exact_rate=mean(
            r["count_error"] == 0 and r["protocol_complete"] and r["answered"]
            for r in rows
            if r["type"] == "COUNT"
        )
        if count
        else None,
        observed_count_agreement_rate=mean(v == 0 for v in count) if count else None,
        count_mean_absolute_error=mean(count) if count else None,
        protocol_complete_rate=avg("protocol_complete"),
        observed_lower_bound_valid_rate=avg("lower_bound_valid"),
        negative_returned_candidate_rate=avg("negative_has_candidate"),
        negative_false_confirmation_rate=avg("negative_false_confirmation"),
        wrong_source_candidate_rate=avg("wrong_source_candidate"),
        wrong_source_confirmation_rate=avg("wrong_source_confirmation"),
        negative_queries=sum("negative_has_candidate" in r for r in rows),
        near_negative_queries=sum("wrong_source_candidate" in r for r in rows),
        positive_grounding_queries=sum("grounded" in r for r in rows),
        point_only_positive_queries=sum(r.get("point_only_positive", False) for r in rows),
        assessed_failure_time_ratio=sum(r.get("failed_us", 0) for r in rows) / requested
        if requested
        else None,
        assessed_unverified_time_ratio=1 - sum(r.get("verified_us", 0) for r in rows) / requested
        if requested
        else None,
        unqualified_global_claims=sum(r["unqualified_global_claims"] for r in rows),
        tenant_leaks=sum(r["tenant_leaks"] for r in rows),
        out_of_scope_results=sum(r["out_of_scope_results"] for r in rows),
        clear_events=sum(r.get("clear_event_count", 0) for r in rows),
        short_events=sum(r.get("short_event_count", 0) for r in rows),
        unmatched_events=sum(r.get("unmatched_events", 0) for r in rows),
        unmatched_predictions=sum(r.get("unmatched_predictions", 0) for r in rows),
        ambiguous_events=sum(r.get("ambiguous_events", 0) for r in rows),
        boundary_matched=sum(len(r["start_errors"]) for r in rows),
        boundary_start_p95_us=percentile([v for r in rows for v in r["start_errors"]], 0.95),
        boundary_end_p95_us=percentile([v for r in rows for v in r["end_errors"]], 0.95),
    )


def evaluate(dataset, predictions, *, dev=None, manifest_sha256=None, audit_paths=()):
    validate_manifest(dataset, dev=dev)
    require(
        all(predictions.get(k) for k in ("commit", "model_versions", "generation", "config_digest")),
        "predictions lack reproducible config",
    )
    predicted = {x["id"]: x for x in predictions["queries"]}
    if dataset["split"] == "BLIND":
        require(
            re.fullmatch("[0-9a-f]{64}", manifest_sha256 or "")
            and predictions.get("manifest_sha256") == manifest_sha256,
            "bind blind predictions to the measured manifest digest",
        )
    require(
        len(predicted) == len(predictions["queries"])
        and set(predicted) == {q["id"] for q in dataset["queries"]},
        "missing, duplicated or extra query predictions",
    )
    groups = defaultdict(list)
    media = {m["id"]: m for m in dataset["media"]}
    rows = []
    for q in dataset["queries"]:
        row = score_query(q, predicted[q["id"]], media, dataset["split"] == "BLIND")
        rows.append(row)
        tags = dict(q["groups"])
        duration = max(media[mid]["duration_us"] for mid in q["scope"])
        tags["video_length"] = "2h+" if duration >= 7200 * US else "1h+" if duration >= 3600 * US else "<1h"
        tags["content_type"] = "+".join(sorted({media[mid]["category"] for mid in q["scope"]}))
        for k, val in tags.items():
            groups[k + "=" + str(val)].append(row)
    metrics = aggregate(rows)
    gates = {}
    for name, target in THRESHOLDS.items():
        val = metrics[name]
        gates[name] = (
            None
            if val is None
            else (
                val <= target
                if name
                in {
                    "boundary_start_p95_us",
                    "boundary_end_p95_us",
                    "tenant_leaks",
                    "out_of_scope_results",
                    "unqualified_global_claims",
                }
                else val >= target
            )
        )
    control_results, missing_controls = control_gates(predictions, audit_paths)
    gates.update(control_results)
    gates["negative_evaluation"] = (
        True if metrics["negative_queries"] or metrics["near_negative_queries"] else None
    )
    global_rows = [r for r in rows if "coverage_audited" in r]
    gates["global_coverage_evaluation"] = (
        True if global_rows and all(r["coverage_audited"] for r in global_rows) else None
    )
    quality_status = status(dataset["split"], gates)
    return dict(
        split=dataset["split"],
        metrics=metrics,
        thresholds=THRESHOLDS,
        gates=gates,
        quality_status=quality_status,
        blind_quality_passed=quality_status == "PASSED",
        missing_operational_cases=missing_controls,
        groups={k: aggregate(v) for k, v in sorted(groups.items())},
        per_query=rows,
    )
