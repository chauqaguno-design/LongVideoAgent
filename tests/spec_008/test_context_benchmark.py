"""Paired comparisons on the same deterministic conversation and constraints."""

import json
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from cutsieve.application.workflow_context import compact_dialogs
from cutsieve.application.workflow_identity import context_estimate, digest_json


def test_compaction_baseline_vs_layered_context(make_checkpoint):
    ids = [str(uuid5(NAMESPACE_URL, f"harness-excerpt-{i}")) for i in range(6)]
    initial = [
        dict(role="system", content="Read the assigned source."),
        dict(role="user", content="保留原声，剪出目标片段。"),
    ]
    checkpoint = make_checkpoint(must_include=["目标片段"], must_exclude=["片尾"])
    brief = checkpoint.spec.brief.model_dump(mode="json")

    def batch(n, large=True):
        raw = dict(
            evidence_ids=[ids[n]],
            text="同一段转写用于对照。This is a passage from the source. " * (160 if large else 1),
        )
        call = f"round-{n}"
        return [
            dict(role="assistant", tool_calls=[dict(id=call)]),
            dict(role="tool", tool_call_id=call, content=json.dumps(raw, ensure_ascii=False)),
        ]

    raw = list(initial)
    layered = list(initial)
    tools = [dict(name="video__get_context_window", description="Read limited source evidence.")]
    for n in (0, 2, 4):
        pair = batch(n) + batch(n + 1, large=False)
        raw += pair
        for key in ids[n : n + 2]:
            checkpoint.context.references[key] = dict(type="EVIDENCE", id=key, produced_by="search")
        layered = compact_dialogs(layered + pair, checkpoint, limit=4500, tools=tools)
        assert layered[-2:] == pair[-2:]

    baseline_refs = json.loads(raw[-1]["content"])["evidence_ids"]
    layer_refs = json.loads(layered[2]["content"])["archived_reference_ids"]
    assert ids[0] not in baseline_refs
    assert set(ids) <= set(layer_refs)
    assert json.loads(layered[2]["content"])["frozen_inputs"]["brief"] == brief
    full_cost, layered_cost = (context_estimate(dialogs, tools)["input_tokens"] for dialogs in (raw, layered))
    assert layered_cost < full_cost
    if output := os.getenv("CUTSIEVE_HARNESS_DEMO_OUT"):
        folder = Path(output)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "context-comparison.json").write_text(
            json.dumps(
                dict(
                    fixture_sha256=digest_json(raw),
                    rounds=3,
                    total_reference_count=len(ids),
                    recent_turn_first_reference_retained=ids[0] in baseline_refs,
                    recent_turn_reference_count=len(baseline_refs),
                    layered_reference_count=len(layer_refs),
                    all_hard_constraints_retained=True,
                    full_estimated_input_tokens=full_cost,
                    layered_estimated_input_tokens=layered_cost,
                    tokenizer="conservative_utf8_byte_estimate",
                    provider_model_calls=0,
                ),
                indent=2,
            ),
            encoding="utf8",
        )
