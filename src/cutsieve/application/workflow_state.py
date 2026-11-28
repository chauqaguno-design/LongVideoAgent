"""Public state, limits and error envelope for workflow runs."""

from datetime import datetime, timezone
from uuid import UUID, uuid5

from .edit_timeline import EditError, uid


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED"}
RUNNABLE = {"QUEUED", "RUNNING", "WAITING_JOB"}
DEADLINE_SECONDS = 300
HARNESS_USAGE_FIELDS = {"images_unloaded", "compactions"}


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def stable_id(run_id, label):
    return str(uuid5(UUID(run_id), label))


def public_error(code, message, retryable=False):
    return dict(code=code, message=message, retryable=retryable, request_id=uid(), details={})


def new_state(run_id, workspace, project, usage, legacy_limits=None):
    limits = legacy_limits or dict(
        max_model_calls=usage.max_steps, max_tool_calls=usage.max_steps, max_images=usage.max_images,
        max_cost_microunits=0, currency="CNY", deadline_seconds=DEADLINE_SECONDS,
    )
    state = dict(
        run_id=run_id, workspace_id=workspace, project_id=project, status="QUEUED",
        phase="AGENT", state_version=0, pending_job_ids=[], result_artifact_id=None,
        error=None, awaiting_input=None, superseded_by_run_id=None,
        last_event_id=None, last_event_sequence=0, cancel_requested_at=None,
        budget_usage=dict(limits=limits, currency=limits["currency"], pricing_status="MISSING",
                          price_version=None, committed_microunits=None, reserved_microunits=None,
                          uncertain_microunits=None, cache_hit_tokens=None),
    )
    sync_usage(state, usage)
    return state


def sync_usage(state, usage):
    counts = usage.model_dump(exclude=HARNESS_USAGE_FIELDS)
    state["usage"] = counts
    budget = state["budget_usage"]
    for key in ("model_calls", "tool_calls", "images", "input_tokens", "output_tokens"):
        budget[key] = counts[key]
    if budget["pricing_status"] == "CONFIGURED" and budget["currency"] == "CNY":
        budget["committed_microunits"] = round(usage.estimated_cost_cny * 1_000_000)


def require_version(state, expected):
    if expected != state["state_version"]:
        raise EditError(409, "STATE_VERSION_CONFLICT", "任务状态已更新，请刷新后重试")
