from types import SimpleNamespace

import pytest

from dovideo.application.workflow_review import repair_for
from dovideo.application.workflow_state import DEFAULT_BUDGET, new_state


@pytest.mark.parametrize("problem", ["limit", "uncertain", "budget", "rejected", "wrong_duration"])
def test_repair_requires_remaining_budget_and_a_new_supported_replacement(problem, make_checkpoint):
    selected = dict(
        clip_id="c2",
        media_revision_id="m",
        source_start_us=3,
        source_end_us=4,
        evidence_ids=["old"],
        audio_policy="keep",
    )
    neighbor = dict(selected, clip_id="c1", source_start_us=0, source_end_us=2, evidence_ids=[])
    timeline = dict(
        revision_id="base",
        revision_no=1,
        clips=[neighbor, selected],
        output_profile={},
        caption_policy="preserve_source",
        total_duration_us=3,
    )
    candidate = dict(
        candidate_id="new",
        media_revision_id="m",
        range=dict(start_us=5, end_us=6),
        support="SUPPORTED",
        reason="Confirmed from source frames",
        evidence_ids=["evidence"],
    )
    cp = make_checkpoint(
        scope=dict(project_id="project", media_revision_ids=["m"]),
        duration_target_us=3, order_policy="SOURCE_ORDER",
    )
    cp.attempt.shot_plan = dict(shots=[dict(target_clip_id="c2", evidence_ids=["old"], candidate_id="initial")])
    cp.attempt.candidate_snapshot = [candidate]
    state = new_state("run", "workspace", "project", dict(DEFAULT_BUDGET, max_cost_microunits=100))
    issue = dict(code="SAMPLED_CLIP_CONFLICT", affected_clip_ids=["c2"], repairability="MANUAL")
    row = SimpleNamespace(checkpoint=cp, state=state)
    possible = repair_for(row, dict(issue), timeline)
    assert possible.attempt.preserved_clip_ids == ["c1"]
    if problem == "limit":
        cp.repair.repair_count = 2
    elif problem == "uncertain":
        issue["code"] = "CONTENT_UNCONFIRMED"
    elif problem == "budget":
        state["budget_usage"]["uncertain_microunits"] = 100
    elif problem == "rejected":
        cp.repair.signatures = [possible.signature]
    else:
        cp.attempt.candidate_snapshot[0]["range"]["end_us"] = 7
    assert repair_for(row, issue, timeline) is None
