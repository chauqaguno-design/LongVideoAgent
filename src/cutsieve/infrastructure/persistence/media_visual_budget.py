"""Record each physical video observation request before it can leave the worker."""

from sqlalchemy import insert, select, update

from .processing_catalog import StaleAttempt, record
from .processing_tables import runs, records
from .workspace_budget_table import workspace_budgets


def cost(tokens, rate):
    return (tokens * rate + 999999) // 1000000


def price_config(env):
    prefix = "CUTSIEVE_FLASH_CNY_"
    input_price = env[prefix + "INPUT_MICROS_PER_MILLION"]
    output_price = env[prefix + "OUTPUT_MICROS_PER_MILLION"]
    version = env["CUTSIEVE_FLASH_PRICE_VERSION"]
    if not version or not all(str(value).isdigit() for value in (input_price, output_price)):
        raise ValueError("Invalid visual pricing")
    return dict(input=int(input_price), output=int(output_price), version=version)


def workspace_balance(db, workspace, currency):
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


def billed_window(catalog, run, unit, payload_bytes, images, env, send):
    currency = "CNY"
    try:
        price = price_config(env)
        run_limit = int(env["CUTSIEVE_MEDIA_VISUAL_MAX_CNY_MICROUNITS"])
        workspace_limit = int(env["CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS"])
    except (KeyError, ValueError):
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
