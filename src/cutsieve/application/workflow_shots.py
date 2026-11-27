"""Compile source-grounded selections and enforce refinement on the complete timeline."""

from .edit_timeline import EditError
from .workflow_identity import generation_key
from .workflow_skills import skill_identity
from .workflow_state import NeedsInput, stable_id


def bind_clip(revision, number):
    clips = revision["clips"]
    if number == -1:
        number = len(clips)
    if not 1 <= number <= len(clips):
        raise NeedsInput(f"当前剪辑只有 {len(clips)} 段，请指定需要替换的段号。")
    selected = clips[number - 1]["clip_id"]
    return dict(
        target_clip_ids=[selected],
        preserved_clip_ids=[c["clip_id"] for c in clips if c["clip_id"] != selected],
    )


def disjoint(clip, mid, span):
    return (
        clip["media_revision_id"] != mid
        or clip["source_end_us"] <= span["start_us"]
        or span["end_us"] <= clip["source_start_us"]
    )


def local_brief(brief, base, target_ids):
    """The original duration selector still sees the duration required of the new material."""
    if base is None or brief["duration_target_us"] is None:
        return brief
    kept = sum(
        c["source_end_us"] - c["source_start_us"] for c in base["clips"] if c["clip_id"] not in target_ids
    )
    target = brief["duration_target_us"] - kept
    if target < 0:
        raise NeedsInput("保留片段已经超过目标总时长。", kind="CONSTRAINT_CONFLICT")
    return dict(brief, duration_target_us=target)


def select_replacement(brief, intervals, base, target_ids, *, alternative=False):
    target = brief["duration_target_us"]
    tolerance = brief["duration_tolerance_us"]
    untouched = [c for c in base["clips"] if alternative or c["clip_id"] not in target_ids]
    for mid, span, refs in intervals:
        if not all(disjoint(c, mid, span) for c in untouched):
            continue
        duration = span["end_us"] - span["start_us"]
        if target is None or abs(duration - target) <= tolerance:
            return [(mid, span, refs)]
    raise NeedsInput("没有可替换且满足时长和来源条件的单一片段。", kind="CONSTRAINT_CONFLICT")


def replace_operation(original, source, *, preserve_audio):
    clip = dict(original, **source)
    clip_id = clip.pop("clip_id")
    if preserve_audio:
        clip["audio_policy"] = original["audio_policy"]
    if original["media_revision_id"] != source["media_revision_id"]:
        clip.pop("caption_track_id", None)
        clip.pop("crop", None)
    return dict(op="replace_clip", clip_id=clip_id, clip=clip)


def distinct_candidates(hits, limit=3):
    seen = set()
    for position, hit in enumerate(hits):
        span = hit["range"]
        key = (hit["media_revision_id"], span["start_us"], span["end_us"])
        if key in seen:
            continue
        seen.add(key)
        yield position, hit
        if len(seen) == limit:
            break


def candidate(row, position, hit, report):
    return dict(
        candidate_id=stable_id(row.id, generation_key(row.checkpoint, f"candidate:{position}")),
        media_revision_id=hit["media_revision_id"],
        range=hit["range"],
        evidence_ids=[report["evidence_id"]] if report.get("evidence_id") else [],
        support=report["support"],
        reason=report["reason"][:500],
    )


def grounded_plan(row, operations):
    cp = row.checkpoint
    shots = []
    for operation in operations:
        clip = operation["clip"]
        span = dict(start_us=clip["source_start_us"], end_us=clip["source_end_us"])
        selected = next(
            (
                c
                for c in cp.attempt.candidate_snapshot
                if c["media_revision_id"] == clip["media_revision_id"]
                and c["range"] == span
                and c["evidence_ids"] == clip["evidence_ids"]
                and c["support"] == "SUPPORTED"
            ),
            None,
        )
        if not selected or not selected["evidence_ids"]:
            raise NeedsInput("镜头方案缺少本轮核实的原片证据。")
        shots.append(dict(selected, order=len(shots) + 1, target_clip_id=operation.get("clip_id")))
    skill_set = cp.attempt.skill_snapshots
    skills = [skill_identity(value) for value in skill_set.values()]
    return dict(
        plan_id=stable_id(row.id, generation_key(cp, f"shot_plan:{cp.attempt.turn}")),
        version=1,
        generation=cp.attempt.plan_generation,
        brief_id=cp.attempt.brief_id,
        base_revision_id=cp.attempt.base_id,
        candidate_snapshot_id=cp.attempt.candidates_id,
        index_generation_id=cp.spec.index_generation_id,
        model=cp.spec.model_identity.model_dump() if cp.spec.model_identity else None,
        skills=skills,
        target_clip_ids=cp.attempt.target_clip_ids,
        preserved_clip_ids=cp.attempt.preserved_clip_ids,
        shots=shots,
    )


def validate_refinement(parent, result, brief, targets, *, grounded=False):
    if targets:
        kept = set(targets)
        before, after = parent["clips"], result["clips"]
        if [c["clip_id"] for c in after] != [c["clip_id"] for c in before] or any(
            a != b for a, b in zip(before, after) if a["clip_id"] not in kept
        ):
            raise EditError(422, "REFINEMENT_SCOPE", "局部修改变更了需要保留的片段")
    total = brief["duration_target_us"]
    if total is not None and abs(result["total_duration_us"] - total) > brief["duration_tolerance_us"]:
        raise NeedsInput("应用修改后的完整时间线不符合目标总时长。", kind="CONSTRAINT_CONFLICT")
    if brief["order_policy"] == "SOURCE_ORDER":
        positions = {key: i for i, key in enumerate(brief["source_media_revision_ids"])}
        order = [(positions[c["media_revision_id"]], c["source_start_us"]) for c in result["clips"]]
        if order != sorted(order):
            raise NeedsInput("修改后会破坏原片时间顺序。", kind="CONSTRAINT_CONFLICT")
    if grounded:
        clips = result["clips"]
        affected = (
            set(targets)
            if targets
            else {c["clip_id"] for c in clips} - {c["clip_id"] for c in parent["clips"]}
        )
        for i, clip in enumerate(clips):
            if clip["clip_id"] not in affected:
                continue
            mid = clip["media_revision_id"]
            span = dict(start_us=clip["source_start_us"], end_us=clip["source_end_us"])
            if any(not disjoint(other, mid, span) for j, other in enumerate(clips) if j != i):
                raise NeedsInput("核实镜头与时间线另一段重叠，请选择不同片段。", kind="CONSTRAINT_CONFLICT")
