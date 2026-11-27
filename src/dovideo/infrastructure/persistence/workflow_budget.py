"""Reserve an upper bound before sending a request; retain uncertain charges after interruption."""

from copy import deepcopy
import time
from urllib.error import URLError
from sqlalchemy import insert, select, update
from dovideo.application.edit_timeline import EditError, uid
from dovideo.application.workflow_identity import (
    context_estimate,
    digest_json,
    generation_key,
    reserved_completion_tokens,
)
from dovideo.application.workflow_state import NeedsInput
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


def review_budget_available(repo, row, env):
    """Use the last review reservation as a preflight; the next actual request still reserves normally."""
    usage = row.state["budget_usage"]
    limits = usage["limits"]
    currency = usage["currency"]
    cap = int(env.get(f"DOVIDEO_WORKSPACE_{currency}_MAX_COST_MICROUNITS", "0"))
    stage_limit = row.checkpoint.spec.stage_model_limit or int(
        env.get("DOVIDEO_WORKFLOW_STAGE_MAX_MODEL_CALLS", limits["max_model_calls"])
    )
    with repo.sessions() as db:
        prior = db.execute(
            select(requests.c.price, requests.c.reserved).where(
                requests.c.run_id == row.id,
            )
        ).all()
        stage = [amount for price, amount in prior if price["budget_scope"] == "REVIEW"]
        exposure = (
            db.execute(
                select(workspace_budgets.c.exposure).where(
                    workspace_budgets.c.workspace == row.workspace_id,
                    workspace_budgets.c.currency == currency,
                )
            ).scalar()
            or 0
        )
    spent = sum(
        usage[k] or 0 for k in ("committed_microunits", "reserved_microunits", "uncertain_microunits")
    )
    return len(stage) < stage_limit and max(stage, default=0) <= min(
        cap - exposure,
        limits["max_cost_microunits"] - spent,
    )


def reserve(repo, claim, step, messages, tool_defs, env, *, identity=None, snapshot=None):
    key = generation_key(claim.checkpoint, step)
    tool_hash = digest_json(tool_defs)
    cp = claim.checkpoint
    manifest = dict(
        scope=cp.spec.scope.model_dump(mode="json"), index_generation_id=cp.spec.index_generation_id,
        brief_id=cp.attempt.brief_id, timeline_revision_id=cp.attempt.revision_id or cp.attempt.base_id,
        shot_plan_id=cp.attempt.shot_plan_id, references=cp.context.references,
    )
    context_hash = digest_json({"messages": messages, "manifest": manifest, "snapshot": snapshot})
    with repo.edits.write() as db:
        row = repo.guard(db, claim)
        phase = row.state["phase"]
        estimate = context_estimate(messages, tool_defs, phase=phase, env=env)
        completion_tokens = estimate["completion_tokens"]
        fingerprint = digest_json(
            {
                "identity": identity,
                "context_hash": context_hash,
                "tools_hash": tool_hash,
                "parameters": {"max_tokens": completion_tokens, "stream": False},
            }
        )
        pred = (requests.c.run_id == row.id, requests.c.step == key)
        history = db.execute(select(requests).where(*pred)).all()
        if any(request.request_fingerprint != fingerprint for request in history):
            raise EditError(409, "MODEL_INPUT_CONFLICT", "逻辑步骤的模型输入已变化，请创建新的方案")
        state = deepcopy(row.state)
        for request in history:
            if request.response is not None:
                repo.trace(
                    db,
                    row,
                    state,
                    "model.replayed",
                    request_id=request.id,
                    logical_step=step,
                    request_fingerprint=fingerprint,
                    reason="persisted_response_matches",
                )
                return None, request.response
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
        images = estimate["images"]
        input_bound = estimate["input_tokens"] + 1024
        reserved = cost(input_bound, price["input"]) + cost(completion_tokens, price["output"])
        used = sum(usage[k] for k in ("committed_microunits", "reserved_microunits", "uncertain_microunits"))
        match, balance = workspace_balance(db, row.workspace_id, usage["currency"])
        setting = f"DOVIDEO_WORKSPACE_{usage['currency']}_MAX_COST_MICROUNITS"
        workspace_limit = int(env.get(setting, 0))
        prices = db.execute(select(requests.c.price).where(requests.c.run_id == row.id)).scalars()
        stage_count = sum(item["budget_scope"] == phase for item in prices)
        price.update(budget_scope=phase, completion_tokens=completion_tokens)
        stage_limit = row.checkpoint.spec.stage_model_limit or int(
            env.get("DOVIDEO_WORKFLOW_STAGE_MAX_MODEL_CALLS", limits["max_model_calls"])
        )
        if (
            used + reserved > limits["max_cost_microunits"]
            or balance + reserved > workspace_limit
            or stage_count >= stage_limit
            or usage["model_calls"] >= limits["max_model_calls"]
            or usage["images"] + images > limits["max_images"]
        ):
            # Preserve reconciliation of earlier interrupted requests even when a retry cannot proceed.
            db.execute(update(runs).where(runs.c.id == row.id).values(state=state))
            return None, NeedsInput(
                "本次调用将超出工作区、工作流或阶段预算，可缩小范围或调整额度。", kind="BUDGET"
            )
        request_id = uid()
        usage["reserved_microunits"] += reserved
        usage["model_calls"] += 1
        usage["images"] += images
        db.execute(
            insert(requests).values(
                id=request_id,
                run_id=row.id,
                step=key,
                price=price,
                reserved=reserved,
                status="PENDING",
                request_fingerprint=fingerprint,
            )
        )
        repo.trace(
            db,
            row,
            state,
            "model.reserved",
            request_id=request_id,
            logical_step=step,
            physical_attempt=len(history) + 1,
            request_fingerprint=fingerprint,
            context_hash=context_hash,
            tool_schema_hash=tool_hash,
            skill=(snapshot or {}).get("skill"),
            budget_scope=phase,
            estimated_input_tokens=estimate["input_tokens"],
            reserved_input_token_bound=input_bound,
            context_estimator="lang-aware-plus-image-allowance",
            completion_reserve_tokens=completion_tokens,
            images=images,
            reserved_microunits=reserved,
            price_version=price["version"],
        )
        db.execute(update(workspace_budgets).where(*match).values(exposure=balance + reserved))
        return (request_id, reserved, price), None


def run_model(repo, claim, step, messages, tool_defs, env, send, *, model=None, identity=None, snapshot=None):
    reservation, previous = reserve(
        repo, claim, step, messages, tool_defs, env, identity=identity or {"model": model}, snapshot=snapshot
    )
    if not reservation:
        if isinstance(previous, NeedsInput):
            raise previous
        return previous
    key, reserved, price = reservation
    result = None
    failure = None
    start = time.monotonic()
    # Carry the reserved limit into the existing zero-argument send callback.
    token = reserved_completion_tokens.set(price["completion_tokens"])
    try:
        result = send()
        for choice in (result or {}).get("choices") or []:
            if isinstance(choice, dict) and isinstance(message := choice.get("message"), dict):
                for field in ("reasoning_content", "reasoning", "reasoning_details", "analysis"):
                    message.pop(field, None)
        return result
    except Exception as exc:
        timed_out = isinstance(exc, TimeoutError) or isinstance(exc, URLError) and isinstance(exc.reason, TimeoutError)
        failure = "MODEL_TIMEOUT" if timed_out else "MODEL_REQUEST_FAILED"
        if timed_out:
            raise EditError(503, failure, "模型请求超时，费用待核对。") from exc
        raise
    finally:
        reserved_completion_tokens.reset(token)
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
                db,
                row,
                state,
                "model.failed" if failure else "model.returned",
                origin=claim,
                request_id=key,
                step=step,
                logical_step=step,
                resolved_model=model,
                error_code=failure,
                latency_ms=latency_ms,
                usage_known=known,
                input_tokens=inp if known else None,
                output_tokens=out if known else None,
                currency=usage["currency"],
                price_version=price["version"],
                charged_microunits=charged if known else None,
                uncertain_microunits=0 if known else reserved,
            )
            if state["cancel_requested_at"] and row.status in ("QUEUED", "RUNNING", "WAITING_JOB"):
                repo.cancelled(db, row, state)
            db.execute(
                update(requests)
                .where(requests.c.id == key)
                .values(status="SETTLED" if known else "UNCERTAIN", response=result)
            )
