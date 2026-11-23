"""Drop whole tool turns while preserving the user's constraints and typed server references."""

import json
from uuid import UUID

from .workflow_state import NeedsInput


def reference_ids(value):
    if isinstance(value, list):
        for item in value:
            yield from reference_ids(item)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {"evidence_ids", "artifact_ids", "observation_ids"} and isinstance(item, list):
                for ident in item:
                    if isinstance(ident, str):
                        try:
                            yield str(UUID(ident))
                        except ValueError:
                            continue
            elif isinstance(item, (dict, list)):
                yield from reference_ids(item)


def archived_ids(messages):
    ids = []
    for message in messages:
        if message["role"] not in ("user", "tool"):
            continue
        try:
            value = json.loads(message["content"])
            if message["role"] == "user":
                value = json.loads(value["untrusted_tool_result"])
        except (ValueError, TypeError, KeyError):
            continue
        ids.extend(reference_ids(value))
    return list(dict.fromkeys(ids))[:100]


def compact_dialogs(messages, checkpoint, limit=24000):
    def size(value):
        return len(json.dumps(value, ensure_ascii=False))

    if size(messages) <= limit:
        return messages
    # Never split the latest native call batch from the tool messages that answer it.
    last = next(
        (i for i in range(len(messages) - 1, 1, -1) if messages[i]["role"] == "assistant"), len(messages)
    )
    keep = ("scope", "base_id", "brief", "clarification", "revision_id", "preview_artifact_id")
    summary = dict(
        context_summary="Previous tool references",
        frozen_inputs={k: checkpoint[k] for k in keep if k in checkpoint},
        archived_reference_ids=archived_ids(messages[2:last]),
    )
    reduced = (
        messages[:2] + [dict(role="user", content=json.dumps(summary, ensure_ascii=False))] + messages[last:]
    )
    if size(reduced) > limit:
        raise NeedsInput("保留来源、指令和最近工具回合后仍超出上下文长度，请缩小素材范围。")
    return reduced
