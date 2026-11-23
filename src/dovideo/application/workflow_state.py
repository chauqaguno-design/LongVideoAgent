"""Public workflow state and the small, fixed set of editing stages."""

from datetime import datetime, timezone
from uuid import UUID, uuid5

from dovideo.application.edit_timeline import EditError, uid


TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "SUPERSEDED"}
RUNNABLE = {"QUEUED", "RUNNING", "WAITING_JOB"}
DEFAULT_BUDGET = dict(
    max_model_calls=8,
    max_tool_calls=64,
    max_images=32,
    max_cost_microunits=0,
    currency="CNY",
    deadline_seconds=900,
)
STAGES = (
    ("PLAN", "COORDINATOR"),
    ("MATERIAL", "MATERIAL"),
    ("PROPOSE", "EDITOR"),
    ("COMMIT", "COORDINATOR"),
    ("RENDER", "COORDINATOR"),
    ("REVIEW", "REVIEWER"),
)


def utc(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def stable_id(run_id, label):
    return str(uuid5(UUID(run_id), label))


def task_label(phase, turn):
    return phase if not turn else f"{phase}:{turn}"


def public_error(code, message, retryable=False):
    return dict(code=code, message=message, retryable=retryable, request_id=uid(), details={})


def new_state(run_id, workspace, project, limits):
    usage = dict(
        limits=limits,
        currency=limits["currency"],
        pricing_status="MISSING",
        price_version=None,
        committed_microunits=None,
        reserved_microunits=None,
        uncertain_microunits=None,
        input_tokens=0,
        output_tokens=0,
        cache_hit_tokens=None,
        images=0,
        model_calls=0,
        tool_calls=0,
    )
    return dict(
        run_id=run_id,
        workspace_id=workspace,
        project_id=project,
        status="QUEUED",
        phase="PLAN",
        state_version=0,
        pending_job_ids=[],
        result_artifact_id=None,
        evidence_ids=[],
        error=None,
        budget_usage=usage,
        awaiting_input=None,
        superseded_by_run_id=None,
        last_event_id=None,
        last_event_sequence=0,
        cancel_requested_at=None,
    )


def require_version(state, expected):
    if expected != state["state_version"]:
        raise EditError(409, "STATE_VERSION_CONFLICT", "任务状态已更新，请刷新后重试")


def budget_change(state, limits):
    usage = state["budget_usage"]
    if limits["currency"] != usage["currency"]:
        raise EditError(422, "BUDGET_CURRENCY", "同一任务须使用相同币种")
    for limit, used in (
        ("max_tool_calls", "tool_calls"),
        ("max_model_calls", "model_calls"),
        ("max_images", "images"),
    ):
        if limits[limit] < usage[used]:
            raise EditError(409, "BUDGET_ALREADY_USED", "额度低于本任务已经使用的数量")
    spent = sum(
        usage[k] or 0 for k in ("committed_microunits", "reserved_microunits", "uncertain_microunits")
    )
    if limits["max_cost_microunits"] < spent:
        raise EditError(409, "BUDGET_ALREADY_USED", "金额低于本任务已用和已预留费用")
    usage["limits"] = limits


class NeedsInput(Exception):
    def __init__(self, question, options=(), kind="CLARIFICATION"):
        self.input = dict(kind=kind, question=question, options=list(options), context_artifact_ids=[])
        super().__init__(question)
