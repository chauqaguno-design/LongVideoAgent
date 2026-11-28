"""Apply an ordered, atomic patch to a copy of an immutable timeline."""
from copy import deepcopy
from datetime import datetime, timezone
from typing import Annotated
from uuid import uuid4

from pydantic import Field

AudioFadeMs = Annotated[int, Field(ge=0, le=3000)]


def uid():
    return str(uuid4())


def timestamp():
    return datetime.now(timezone.utc).isoformat()


class EditError(Exception):
    def __init__(self, status, code, message):
        self.status, self.code = status, code
        super().__init__(message)


def invalid(message):
    return EditError(422, "INVALID_TIMELINE", message)


def missing():
    return EditError(404, "NOT_FOUND", "资源不存在")


def empty_revision(workspace, edit_id, profile):
    return dict(revision_id=uid(), edit_project_id=edit_id, workspace_id=workspace, revision_no=0,
                parent_revision_id=None, created_at=timestamp(), output_profile=profile, clips=[],
                caption_policy="none", audio_fade_ms=0, total_duration_us=0, source_brief_id=None)


def revision_view(row):
    return {"audio_fade_ms": 0, **row.snapshot}


def apply_patch(parent, operations, load_revision, validate_clip):
    result = deepcopy(parent)
    result.setdefault("audio_fade_ms", 0)
    assignments = []
    for index, operation in enumerate(operations):
        op = operation["op"]
        clips = result["clips"]
        if op == "append_clip":
            clip_id = uid()
            clips.append(dict(operation["clip"], clip_id=clip_id))
            assignments.append(dict(operation_index=index, clip_id=clip_id))
        elif op in ("remove_clip", "replace_clip"):
            pos = next((i for i, c in enumerate(clips) if c["clip_id"] == operation["clip_id"]), None)
            if pos is None:
                raise invalid("片段不在此时间线中")
            if op == "remove_clip":
                clips.pop(pos)
            else:
                clips[pos] = dict(operation["clip"], clip_id=operation["clip_id"])
        elif op == "reorder_clips":
            by_id = {c["clip_id"]: c for c in clips}
            order = operation["clip_ids"]
            if len(order) != len(clips) or set(order) != by_id.keys():
                raise invalid("排序必须包含每个片段一次")
            result["clips"] = [by_id[k] for k in order]
        elif op == "set_output_profile":
            result["output_profile"] = operation["output_profile"]
        elif op == "set_caption_policy":
            result["caption_policy"] = operation["caption_policy"]
        elif op == "set_audio_fade":
            fade = operation["audio_fade_ms"]
            if type(fade) is not int or not 0 <= fade <= 3000:
                raise invalid("audio_fade_ms must be an integer from 0 to 3000")
            result["audio_fade_ms"] = fade
        elif op == "restore_revision":
            previous = load_revision(operation["source_revision_id"])
            for field in ("clips", "output_profile", "caption_policy", "source_brief_id"):
                result[field] = deepcopy(previous[field])
            result["audio_fade_ms"] = previous.get("audio_fade_ms", 0)
        else:
            raise invalid("未知的编辑操作")
    if len(result["clips"]) > 200:
        raise invalid("每个版本最多 200 个片段")
    for clip in result["clips"]:
        validate_clip(clip)
    result.update(revision_id=uid(), parent_revision_id=parent["revision_id"],
                  revision_no=parent["revision_no"] + 1, created_at=timestamp(),
                  total_duration_us=sum(c["source_end_us"] - c["source_start_us"] for c in result["clips"]))
    return result, assignments
