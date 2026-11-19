"""Localize action captions for reading while keeping the indexed evidence intact."""

from .pipeline import scene_summary
from .translation import needs_translation, translate_action


def chinese_actions(context, translate=None):
    translate = translate or translate_action
    display = []
    changed = set()
    for row in context["evidence"]:
        text = row["text"]
        if row["track"] == "visual" and needs_translation(text):
            try:
                chinese = translate(text)
            except (OSError, ValueError, RuntimeError, ImportError):
                row = {**row, "translation_error": "中文翻译暂不可用，当前显示模型原文。"}
            else:
                row = {**row, "text": chinese, "original_text": text,
                       "source": row["source"].replace("英文画面观察", "画面观察")}
                changed.add(row["id"])
        display.append(row)

    by_id = {row["id"]: row for row in display}
    scenes = []
    for scene in context["scenes"]:
        refs = scene["evidence_ids"]
        if changed.intersection(refs):
            scene = {**scene, "summary": scene_summary(by_id[key] for key in refs)}
        scenes.append(scene)
    tracks = dict(context.get("tracks", {}))
    visual = tracks.get("visual", {})
    if "refresh" in visual:
        pending = visual["refresh"]
        tracks["visual"] = {"status": "processing", "progress": pending.get("progress", 0),
                            "message": "正在重新分析画面，完成后更新证据。"}
    return {**context, "tracks": tracks, "evidence": display, "scenes": scenes}
