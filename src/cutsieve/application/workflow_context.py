"""Drop whole tool turns while preserving the user's constraints and typed server references."""

import json

from .edit_timeline import missing
from .workflow_checkpoint import Checkpoint
from .workflow_identity import context_estimate
from .workflow_state import NeedsInput


def remember_outputs(checkpoint: Checkpoint, phase: str):
    attempt = checkpoint.attempt
    refs = checkpoint.context.references
    timeline = attempt.revision_id or attempt.base_id
    if timeline:
        refs.setdefault(timeline, dict(type="TIMELINE", id=timeline, produced_by=phase, timeline_revision_id=timeline))
    for key in (
        attempt.brief_id, attempt.candidates_id, attempt.shot_plan_id,
        attempt.preview_artifact_id, attempt.export_artifact_id, attempt.preview_review_id, attempt.export_review_id,
    ):
        if key:
            refs.setdefault(key, dict(
                type="ARTIFACT", id=key, produced_by=phase, timeline_revision_id=timeline,
                index_generation_id=checkpoint.spec.index_generation_id,
            ))


def remember_evidence(repo, row, references, ids, step, *, session=None):
    """Record the identity of evidence returned by an authorized server operation."""
    refs = dict(references)
    for key in dict.fromkeys(ids):
        if key in refs:
            continue
        item = repo.edits.resolve_evidence(row.workspace_id, key, session=session)
        if (
            item["workspace_id"] != row.workspace_id
            or item["project_id"] != row.project_id
            or item["media_revision_id"] not in row.checkpoint.spec.scope.media_revision_ids
        ):
            raise missing()
        refs[key] = dict(
            type="EVIDENCE",
            id=key,
            media_revision_id=item["media_revision_id"],
            index_generation_id=row.checkpoint.spec.index_generation_id,
            source_generation_id=item["index_generation_id"],
            range=item["range"],
            produced_by=step,
        )
    return refs


def read_reference(repo, row, key, *, references=None):
    """Reauthorize the immutable, server-issued reference on every rehydrate."""
    refs = row.checkpoint.context.references if references is None else references
    ref = refs.get(key)
    if not ref or ref["type"] != "EVIDENCE":
        raise missing()
    repo.sources.media(row.workspace_id, ref["media_revision_id"])
    item = repo.edits.resolve_evidence(row.workspace_id, key)
    if (
        item["workspace_id"] != row.workspace_id
        or item["project_id"] != row.project_id
        or item["media_revision_id"] != ref["media_revision_id"]
        or item["media_revision_id"] not in row.checkpoint.spec.scope.media_revision_ids
        or item["index_generation_id"] != ref["source_generation_id"]
        or item["range"] != ref["range"]
    ):
        raise missing()
    return item


def compact_dialogs(messages, checkpoint: Checkpoint, limit=24000, *, tools=()):
    def size(value):
        estimate = context_estimate(value, tools)
        return estimate["input_tokens"] + estimate["completion_tokens"]

    if size(messages) <= limit:
        return messages
    # Never split the latest native call batch from the tool messages that answer it.
    last = next(
        (i for i in range(len(messages) - 1, 1, -1) if messages[i]["role"] == "assistant"), len(messages)
    )
    attempt = checkpoint.attempt
    frozen = dict(
        scope=checkpoint.spec.scope.model_dump(mode="json"),
        brief=checkpoint.spec.brief.model_dump(mode="json") if checkpoint.spec.brief else None,
        base_id=attempt.base_id, clarification=attempt.clarification,
        revision_id=attempt.revision_id, preview_artifact_id=attempt.preview_artifact_id,
    )
    summary = dict(
        context_summary="Previous tool references",
        frozen_inputs={key: value for key, value in frozen.items() if value is not None},
    )
    ids = list(checkpoint.context.references)
    summary.update(
        archived_reference_ids=ids[:32],
        reference_count=len(ids),
        index_generation_id=checkpoint.spec.index_generation_id,
    )
    reduced = messages[:2] + [dict(role="user", content="")] + messages[last:]
    while True:
        reduced[2]["content"] = json.dumps(summary, ensure_ascii=False)
        if size(reduced) <= limit:
            return reduced
        shown = summary["archived_reference_ids"]
        if not shown:
            break
        summary["archived_reference_ids"] = shown[: len(shown) // 2]
    raise NeedsInput("保留来源、指令和最近工具回合后仍超出上下文长度，请缩小素材范围。")


def tool_preview(name, value):
    limits = {
        "hits": 4,
        "clips": 12,
        "evidence_ids": 16,
        "artifact_ids": 16,
        "observation_ids": 16,
        "warnings": 3,
    }
    preview = {}
    clipped = {}
    keys = (
        ("query_id", "index_generation_id", "hits", "warnings") if name == "video.search_evidence" else value
    )
    for key in keys:
        if key not in value:
            continue
        item = value[key]
        limit = limits.get(key)
        if limit is not None and len(item) > limit:
            clipped[key] = len(item)
            item = item[:limit]
        preview[key] = item
    return preview, clipped


def append_tool_dialog(dialogs, mode, call_id, value):
    content = json.dumps(value, ensure_ascii=False)
    if mode == "native":
        dialogs.append(dict(role="tool", tool_call_id=call_id, content=content))
    else:
        dialogs.append(
            dict(
                role="user",
                content=json.dumps(
                    {"untrusted_tool_result": content},
                    ensure_ascii=False,
                ),
            )
        )
