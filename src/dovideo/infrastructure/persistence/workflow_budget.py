"""Reserve an upper bound before sending a request; retain uncertain charges after interruption."""

from copy import deepcopy
import json
import time
from urllib.error import URLError
from sqlalchemy import insert, select, update
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.workflow_state import NeedsInput, stable_id, task_label
from .workflow_tables import model_requests as requests, runs
from .workspace_budget_table import workspace_budgets


def cost(tokens, rate):
    return (tokens * rate + 999999) // 1000000


def price_config(env, currency):
    prefix = "DOVIDEO_FLASH_" + currency + "_"
    keys = (prefix + "INPUT_MICROS_PER_MILLION", prefix + "OUTPUT_MICROS_PER_MILLION")
    prices = [env.get(k, "") for k in keys]
    if not all(str(p).isdigit() for p in prices) or not env.get("DOVIDEO_FLASH_PRICE_VERSION"):
        raise NeedsInput("缺少此币种的模型计价配置，无法在费用上限内发起调用。", kind="BUDGET")
    return dict(input=int(prices[0]), output=int(prices[1]), version=env["DOVIDEO_FLASH_PRICE_VERSION"])


def workspace_balance(db, workspace, currency):
    """First-use insertion and balance inspection are serialized for each workspace and currency."""
    fields = dict(workspace=workspace, currency=currency, exposure=0)
    if db.get_bind().dialect.name == "mysql":
        from sqlalchemy.dialects.mysql import insert as insert_mysql

        statement = insert_mysql(workspace_budgets).values(**fields)
        db.execute(statement.on_duplicate_key_update(workspace=statement.inserted.workspace))
    else:
        from sqlalchemy.dialects.sqlite import insert as insert_sqlite

        db.execute(insert_sqlite(workspace_budgets).values(**fields).on_conflict_do_nothing())
    match = (workspace_budgets.c.workspace == workspace, workspace_budgets.c.currency == currency)
    amount = db.execute(select(workspace_budgets.c.exposure).where(*match).with_for_update()).scalar_one()
    return match, amount


def reserve(repo, claim, step, messages, tool_defs, env):
    with repo.edits.write() as db:
        row = repo.guard(db, claim)
        pred = (requests.c.run_id == row.id, requests.c.step == step)
        history = db.execute(select(requests).where(*pred)).all()
        for request in history:
            if request.response is not None:
                return None, request.response
        state = deepcopy(row.state)
        usage = state["budget_usage"]
        limits = usage["limits"]
        price = price_config(env, usage["currency"])
        if usage["price_version"] not in (None, price["version"]):
            raise NeedsInput("本次运行已固定价格版本，请使用原定价配置或新建任务。", kind="BUDGET")
        usage.update(pricing_status="CONFIGURED", price_version=price["version"])
        for name in ("committed_microunits", "reserved_microunits", "uncertain_microunits"):
            if usage[name] is None:
                usage[name] = 0
        for prior in history:
            if prior.status == "PENDING":
                usage["reserved_microunits"] -= prior.reserved
                usage["uncertain_microunits"] += prior.reserved
                db.execute(update(requests).where(requests.c.id == prior.id).values(status="UNCERTAIN"))
        size = len(json.dumps([messages, tool_defs], ensure_ascii=False).encode()) + 1024
        images = sum(
            1
            for message in messages
            if isinstance(message.get("content"), list)
            for part in message["content"]
            if part.get("type") == "image_url"
        )
        reserved = cost(size + 2048 * images, price["input"]) + cost(512, price["output"])
        used = sum(usage[k] for k in ("committed_microunits", "reserved_microunits", "uncertain_microunits"))
        match, balance = workspace_balance(db, row.workspace_id, usage["currency"])
        setting = f"DOVIDEO_WORKSPACE_{usage['currency']}_MAX_COST_MICROUNITS"
        workspace_limit = int(env.get(setting, 0))
        task_id = (
            stable_id(row.id, task_label(row.state["phase"], row.checkpoint["turn"]))
            if row.checkpoint["team"]
            else None
        )
        task_count = 0
        if task_id:
            prices = db.execute(select(requests.c.price).where(requests.c.run_id == row.id)).scalars()
            task_count = sum(item.get("task_id") == task_id for item in prices)
            price["task_id"] = task_id
        task_limit = int(env.get("DOVIDEO_WORKFLOW_TASK_MAX_MODEL_CALLS", limits["max_model_calls"]))
        if (
            used + reserved > limits["max_cost_microunits"]
            or balance + reserved > workspace_limit
            or (task_id and task_count >= task_limit)
            or usage["model_calls"] >= limits["max_model_calls"]
            or usage["images"] + images > limits["max_images"]
        ):
            # Preserve reconciliation of earlier interrupted requests even when a retry cannot proceed.
            db.execute(update(runs).where(runs.c.id == row.id).values(state=state))
            return None, NeedsInput(
                "本次调用将超出工作区、工作流或阶段预算，可缩小范围或调整额度。", kind="BUDGET"
            )
        key = uid()
        usage["reserved_microunits"] += reserved
        usage["model_calls"] += 1
        usage["images"] += images
        db.execute(
            insert(requests).values(
                id=key, run_id=row.id, step=step, price=price, reserved=reserved, status="PENDING"
            )
        )
        repo.trace(
            db,
            row,
            state,
            "model.reserved",
            request_id=key,
            images=images,
            reserved_microunits=reserved,
            price_version=price["version"],
        )
        db.execute(update(workspace_budgets).where(*match).values(exposure=balance + reserved))
        return (key, reserved, price), None


def run_model(repo, claim, step, messages, tool_defs, env, send, *, model=None):
    reservation, previous = reserve(repo, claim, step, messages, tool_defs, env)
    if not reservation:
        if isinstance(previous, NeedsInput):
            raise previous
        return previous
    key, reserved, price = reservation
    result = None
    failure = None
    start = time.monotonic()
    try:
        result = send()
        return result
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
        failure = "MODEL_TIMEOUT" if timed_out else "MODEL_REQUEST_FAILED"
        if timed_out:
            raise EditError(503, failure, "模型请求超时，费用待核对。") from exc
        raise
    finally:
        # Even after cancellation, this paid physical attempt belongs to its original run.
        latency_ms = int((time.monotonic() - start) * 1000)
        with repo.edits.write() as db:
            row = db.execute(select(runs).where(runs.c.id == claim.id).with_for_update()).one()
            state = deepcopy(row.state)
            req = db.execute(select(requests).where(requests.c.id == key)).first()
            usage = state["budget_usage"]
            usage["uncertain_microunits" if req.status == "UNCERTAIN" else "reserved_microunits"] -= reserved
            reported = (result or {}).get("usage") or {}
            inp, out = reported.get("prompt_tokens"), reported.get("completion_tokens")
            known = all(type(v) is int and v >= 0 for v in (inp, out))
            if known:
                charged = cost(inp, price["input"]) + cost(out, price["output"])
                usage["input_tokens"] += inp
                usage["output_tokens"] += out
                usage["committed_microunits"] += charged
                cached = (reported.get("prompt_tokens_details") or {}).get("cached_tokens")
                if type(cached) is int and 0 <= cached <= inp:
                    usage["cache_hit_tokens"] = (usage["cache_hit_tokens"] or 0) + cached
                match, balance = workspace_balance(db, claim.workspace_id, usage["currency"])
                db.execute(
                    update(workspace_budgets).where(*match).values(exposure=balance - reserved + charged)
                )
            else:
                usage["uncertain_microunits"] += reserved
            repo.trace(
                db, row, state, "model.failed" if failure else "model.returned", origin=claim,
                request_id=key, step=step, resolved_model=model, error_code=failure,
                latency_ms=latency_ms,
                usage_known=known, input_tokens=inp if known else None, output_tokens=out if known else None,
                currency=usage["currency"], price_version=price["version"],
                charged_microunits=charged if known else None, uncertain_microunits=0 if known else reserved,
            )
            if state["cancel_requested_at"] and row.status in ("QUEUED", "RUNNING", "WAITING_JOB"):
                repo.cancelled(db, row, state)
            db.execute(
                update(requests)
                .where(requests.c.id == key)
                .values(status="SETTLED" if known else "UNCERTAIN", response=result)
            )
