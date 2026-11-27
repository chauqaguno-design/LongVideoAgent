"""Record each physical video observation request before it can leave the worker."""

from sqlalchemy import insert, select, update

from cutsieve.application.workflow_state import NeedsInput
from .processing_catalog import StaleAttempt, record
from .processing_tables import runs, records
from .workflow_budget import cost, price_config, workspace_balance
from .workspace_budget_table import workspace_budgets


def billed_window(catalog, run, unit, payload_bytes, images, env, send):
    currency = "CNY"
    try:
        price = price_config(env, currency)
        run_limit = int(env["CUTSIEVE_MEDIA_VISUAL_MAX_CNY_MICROUNITS"])
        workspace_limit = int(env["CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS"])
    except (NeedsInput, KeyError, ValueError):
        raise ValueError("视觉调用缺少定价或费用上限") from None
    # The serialized payload includes the JPEG data. Bound text separately from the images.
    text_tokens = min(payload_bytes, 4096)
    reserved = cost(text_tokens + images * 2048, price["input"]) + cost(768, price["output"])
    entry = record(
        run,
        "VISUAL_CHARGE",
        unit,
        dict(
            status="UNCERTAIN",
            currency=currency,
            price=price,
            reserved=reserved,
            charged=None,
            images=images,
            usage=None,
        ),
    )
    with catalog.write() as db:
        if not db.execute(select(runs.c.id).where(catalog.current(run)).with_for_update()).first():
            raise StaleAttempt()
        prior = db.execute(
            select(records.c.unit, records.c.body).where(
                records.c.run == run.id, records.c.kind == "VISUAL_CHARGE"
            )
        ).all()
        if any(p.unit == unit for p in prior):
            raise ValueError("该视觉窗口已经发起过请求")
        previous = sum(
            p.body["reserved"] if p.body["charged"] is None else p.body["charged"] for p in prior
        )
        match, balance = workspace_balance(db, run.workspace, currency)
        if (
            min(run_limit, workspace_limit) <= 0
            or previous + reserved > run_limit
            or balance + reserved > workspace_limit
        ):
            raise ValueError("本次视觉请求超过可用预算")
        db.execute(insert(records).values(**entry))
        db.execute(update(workspace_budgets).where(*match).values(exposure=balance + reserved))
    # If the network fails after dispatch or the process dies, keep the maximum exposure.
    raw = send()
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if isinstance(usage, dict):
        prompt, answer = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if all(type(n) is int and n >= 0 for n in (prompt, answer)):
            charged = cost(prompt, price["input"]) + cost(answer, price["output"])
            with catalog.write() as db:
                match, balance = workspace_balance(db, run.workspace, currency)
                body = dict(entry["body"], status="SETTLED", charged=charged, usage=usage)
                db.execute(update(records).where(records.c.id == entry["id"]).values(body=body))
                db.execute(
                    update(workspace_budgets).where(*match).values(exposure=balance - reserved + charged)
                )
    return raw
