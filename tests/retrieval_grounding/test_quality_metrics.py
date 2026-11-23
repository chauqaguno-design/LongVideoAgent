"""Hand-checked event, image, boundary and global denominators; no release corpus is fabricated."""

from copy import deepcopy
import pytest
from dovideo.evaluation.video_grounding import GROUPS, US, evaluate, validate_manifest


def event(key, start, end):
    return dict(
        id=key,
        media="m",
        start_us=start * US,
        end_us=end * US,
        basis="known source fixture",
        boundary="CLEAR",
    )


def query(key, kind, events):
    return dict(
        id=key,
        type=kind,
        scope=["m"],
        query="fixture",
        event_definition="one visible marker",
        annotator="fixture generator",
        reviewer="source/time checker",
        groups=dict.fromkeys(GROUPS, "fixture"),
        exhaustive=True,
        dedup_rule="one source instance",
        events=events,
    )


def prediction(key, candidates, grounded, *, count=0):
    return dict(
        id=key,
        candidates=candidates,
        grounded=grounded,
        claims=[],
        answered=bool(grounded),
        tenant_leaks=0,
        coverage="PARTIAL",
        claims_exhaustive=False,
        count=count,
    )


def fixture():
    e1, e2 = event("a", 1, 4), event("b", 7, 10)
    ds = dict(
        split="DEV",
        media=[
            dict(
                id="m",
                duration_us=100 * US,
                sha256="1" * 64,
                category="sports_action",
                license="generated fixture",
                origin_id="synthetic/green",
            )
        ],
        queries=[
            query("two", "TEXT", [e1, e1, e2]),
            query("only", "TEXT", [e1]),
            query("image", "IMAGE", [dict(event("frame", 5, 5), boundary="FRAME")]),
            query("all", "ALL", [e1, e2]),
            query("first", "FIRST", [e1]),
            query("count", "COUNT", [e1, e2]),
        ],
    )
    predictions = dict(
        commit="source",
        generation="generation",
        config_digest="frozen",
        model_versions=["fixture"],
        queries=[
            prediction("two", [e1], [dict(e1, end_us=3 * US)]),
            prediction("only", [e1], []),
            prediction(
                "image",
                [
                    dict(media="foreign", actual_us=5 * US),
                    dict(media="m", actual_us=7 * US),
                ],
                [],
            ),
            prediction("all", [], [e1, e1]),
            prediction("first", [], [e1]),
            prediction("count", [], [e1], count=1),
        ],
    )
    return ds, predictions


def test_frozen_denominators_groups_and_boundaries():
    d, p = fixture()
    p["queries"][0]["claims"] = [dict(supported=True, reviewer="judge")]
    p["queries"][1]["claims"] = [dict(supported=False, reviewer="judge")]
    p["queries"][-1]["claims_exhaustive"] = True
    report = evaluate(d, p)
    m = report["metrics"]
    assert (
        m["text_recall_at_20"] == 0.75 and m["image_recall_at_20"] == 1 and m["grounding_r5_iou50"] == 1 / 3
    )
    assert m["all_precision"] == 0.5 and m["all_recall"] == 0.5 and m["first_correct_rate"] == 1
    assert m["count_mean_absolute_error"] == 1 and m["count_exact_rate"] == 0 and m["claim_precision"] == 0.5
    assert m["boundary_end_p95_us"] == US and m["unqualified_global_claims"] == 1
    assert report["groups"]["content_type=sports_action"] == m
    assert not report["blind_quality_passed"]


def test_no_wrong_media_or_long_candidate_credit_and_missing_predictions():
    d, p = fixture()
    p["queries"][0]["candidates"] = [dict(media="m", start_us=0, end_us=60 * US)]
    p["queries"][2]["candidates"] = [dict(media="foreign", actual_us=5 * US)]
    m = evaluate(d, p)["metrics"]
    assert m["image_recall_at_20"] == 0 and m["text_recall_at_20"] == 0.5
    p["queries"].pop()
    with pytest.raises(ValueError, match="missing"):
        evaluate(d, p)


def test_reencoded_source_cannot_leak_into_blind_and_disputed_gt_rejected():
    d, _ = fixture()
    blind = deepcopy(d)
    blind.update(
        split="BLIND",
        annotation_protocol="independent-human-review",
        frozen_at="2026-11-23",
    )
    blind["media"][0].update(sha256="9" * 64, rights_ref="fixture only")
    with pytest.raises(ValueError, match="source content"):
        validate_manifest(blind, dev=d)
    d["queries"][0]["events"].append(dict(d["queries"][0]["events"][0], end_us=8 * US))
    with pytest.raises(ValueError, match="different meaning"):
        validate_manifest(d)


def test_negative_answer_without_interval_and_failed_prefix_are_counted():
    d, p = fixture()
    q = d["queries"][1]
    q.update(events=[], answerable=False)
    p["queries"][1].update(answered=True, answer_review={"confirms_match": True})
    first = p["queries"][-2]
    first.update(
        coverage="PROTOCOL_COMPLETE",
        claims_exhaustive=True,
        coverage_review=dict(
            reviewer="human",
            basis="watched original",
            verified=[],
            failed=[dict(media="m", start_us=0, end_us=US)],
        ),
    )
    m = evaluate(d, p)["metrics"]
    assert m["negative_false_confirmation_rate"] == 1 and m["unqualified_global_claims"] == 1
    assert m["assessed_failure_time_ratio"] == 0.25 and m["assessed_unverified_time_ratio"] == 1
    assert m["negative_queries"] == 1 and m["positive_grounding_queries"] == 2
    assert m["out_of_scope_results"] == 1 and m["answer_coverage"] == 0.8
    first["coverage_review"].update(failed=[], verified=[dict(media="m", start_us=0, end_us=4 * US)])
    assert not evaluate(d, p)["metrics"]["unqualified_global_claims"]


def test_alternate_interval_and_twenty_distinct_candidates():
    d, p = fixture()
    e = d["queries"][1]["events"][0]
    alternate = dict(event("alt", 14, 18))
    e["alternatives"] = [alternate]
    irrelevant = event("bad", 33, 35)
    p["queries"][1].update(candidates=[irrelevant] * 20 + [alternate], grounded=[alternate])
    row = evaluate(d, p)["per_query"][1]
    assert row["recall"] == 1 and row["grounded"] and row["start_errors"] == [0] and row["end_errors"] == [0]


def test_operational_controls_cannot_pass_without_measured_receipts(tmp_path):
    import hashlib, json
    from dovideo.evaluation.release_audits import CONTROLS, control_gates, status

    assert status("BLIND", {"score": True, "control": None}) == "BLOCKED"
    assert status("BLIND", {"score": False, "control": None}) == "FAILED"
    checks = [
        dict(control=category, case=name, tested=1, violations=0, workspaces=["a", "b"])
        for category, names in CONTROLS.items()
        for name in names
    ]
    receipt = tmp_path / "controls.json"
    p = {"commit": "fixture"}

    def submit():
        receipt.write_text(
            json.dumps(dict(commit="fixture", test_run="pytest-fixture", checks=checks)), encoding="utf8"
        )
        p["audit_evidence"] = [hashlib.sha256(receipt.read_bytes()).hexdigest()]
        return control_gates(p, [receipt])

    gates, gaps = submit()
    assert not gaps and all(gates.values())
    checks.pop()
    checks[0]["violations"] = 1
    gates, gaps = submit()
    assert gates["tenant_isolation"] is False and gates["idempotent_effects"] is None and gaps
    receipt.write_bytes(receipt.read_bytes() + b" ")
    with pytest.raises(ValueError, match="digest"):
        control_gates(p, [receipt])


def test_scorer_replaces_stale_pass_on_invalid_input(tmp_path):
    import json, subprocess, sys
    from pathlib import Path

    out = tmp_path / "score.json"
    out.write_text('{"blind_quality_passed":true}', encoding="utf8")
    f = tmp_path / "missing"
    cli = Path(__file__).resolve().parents[2] / "scripts" / "score_grounding.py"
    result = subprocess.run(
        [sys.executable, str(cli), str(f), str(f), "--output", str(out)], capture_output=True
    )
    actual = json.loads(out.read_bytes())
    assert (
        result.returncode == 2
        and actual["quality_status"] == "BLOCKED"
        and not actual["blind_quality_passed"]
    )


def test_failed_execution_stays_in_denominator_and_rejects_stale_proof():
    d, p = fixture()
    p["queries"][0].update(execution_status="FAILED", claims=[dict(supported=True, reviewer="reviewer")])
    first = p["queries"][-2]
    first.update(
        execution_status="CANCELLED",
        coverage="PROTOCOL_COMPLETE",
        claims_exhaustive=True,
        coverage_review=dict(
            reviewer="judge",
            basis="checked prefix",
            verified=[dict(media="m", start_us=0, end_us=4 * US)],
            failed=[],
        ),
    )
    report = evaluate(d, p)
    assert report["metrics"]["failed_queries"] == 2
    assert report["per_query"][0]["recall"] == 0 and not report["per_query"][0]["supported_claims"]
    assert report["per_query"][-2]["unqualified_global_claims"] == 1
