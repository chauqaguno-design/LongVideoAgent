"""Dependencies and operations shared by the four workflow stages."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..edit_timeline import EditError
from ..workflow_checkpoint import PlanAttempt, RunSpec
from ..workflow_identity import generation_key
from ..workflow_state import NeedsInput
from ...infrastructure.persistence.edit_tables import revisions

if TYPE_CHECKING:
    from ..evidence_search import EvidenceSearch
    from ...infrastructure.persistence.render_worker import RenderWorker
    from ...infrastructure.persistence.workflow_repository import WorkflowRepository
    from ...infrastructure.providers.workflow_flash import FlashPlanner


@dataclass(frozen=True)
class StageDeps:
    repo: WorkflowRepository
    search: EvidenceSearch | None
    planner: FlashPlanner
    render_worker: RenderWorker | None = None


def step(phase, *, attempt: PlanAttempt | None = None, spec: RunSpec | None = None,
         references=None, processing_job_ids=(), status="RUNNING", event="PROGRESS", payload=None,
         docs=None, **state):
    return dict(
        state=dict(phase=phase, status=status, **state),
        attempt=attempt, spec=spec, references=references, processing_job_ids=processing_job_ids,
        event=event,
        payload=payload,
        docs=docs or {},
    )


def call_tool(deps, row, step_key, name, args, execute):
    key = generation_key(row.checkpoint, f"{row.checkpoint.attempt.turn}:{step_key}")
    return deps.repo.invoke(row, key, name, args, execute)


def pin_index(deps, row):
    spec = row.checkpoint.spec
    if spec.index_generation_id is not None or deps.search is None:
        return None
    generation = deps.search.catalog.pin_generation(row.workspace_id, row.project_id)
    return spec.pin_index(generation) if generation else None


def base_revision(deps, row):
    cp = row.checkpoint
    if cp.attempt.base_id:
        return deps.repo.edits.get(
            revisions, row.workspace_id, cp.attempt.base_id, lambda r: r.snapshot, edit_id=cp.attempt.edit_id
        )


def visual_identity(planner):
    if planner.env.get("CUTSIEVE_WORKFLOW_LIVE_MODEL") != "1":
        return None
    try:
        return planner.identity(vision=True)
    except EditError as exc:
        if exc.code in {"VISION_UNAVAILABLE", "CAPABILITY_UNAVAILABLE"}:
            return None
        raise


def source_proofs(repo, row, mid, span, ids):
    if mid not in row.checkpoint.spec.scope.media_revision_ids:
        raise EditError(404, "NOT_FOUND", "所选来源不在本次任务范围内")
    media = repo.sources.media(row.workspace_id, mid)
    if not 0 <= span["start_us"] < span["end_us"] <= media["duration_us"]:
        raise NeedsInput("所选区间已失效，请重新选择原片依据。")
    proofs = {eid: repo.edits.resolve_evidence(row.workspace_id, eid) for eid in ids}
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
