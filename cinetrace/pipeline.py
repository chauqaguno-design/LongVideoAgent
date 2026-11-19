"""Build playable evidence, retrieval windows and a scene directory for one film."""

from pathlib import Path
from tempfile import TemporaryDirectory

from .media import Unavailable, probe, speech_batches, subtitle_batches, visual_batches


TRACKS = {"subtitles": ("字幕", subtitle_batches), "visual": ("画面动作", visual_batches),
          "speech": ("语音", speech_batches)}


def scene_summary(quotes):
    return " · ".join(f"{TRACKS[row['track']][0]}：{row['text'][:120]}" for row in quotes)


def timeline(duration, evidence):
    """Choose boundaries close to a sentence end and link crossing evidence to both windows."""
    chunks, scenes = [], []
    by_id = {row["id"]: row for row in evidence}
    boundaries = sorted({row["end_ms"] for row in evidence})
    begin = 0
    while begin < duration:
        target = min(begin + 30_000, duration)
        nearby = [t for t in boundaries if begin + 20_000 <= t <= min(duration, begin + 40_000)]
        end = min(nearby, key=lambda t: abs(t - target)) if nearby and target < duration else target
        ids = [row["id"] for row in evidence if row["start_ms"] < end and row["end_ms"] > begin]
        chunks.append({"id": f"chunk-{begin}", "start_ms": begin, "end_ms": end, "evidence_ids": ids})
        begin = end

    for i in range(0, len(chunks), 8):
        group = chunks[i:i + 8]
        ids = list(dict.fromkeys(key for chunk in group for key in chunk["evidence_ids"]))
        quotes = []
        seen = {track: set() for track in TRACKS}
        for key in ids:
            row = by_id[key]
            track = row["track"]
            if len(seen[track]) < (3 if track == "visual" else 1) and row["text"] not in seen[track]:
                quotes.append(row)
                seen[track].add(row["text"])
        summary = scene_summary(quotes)
        scenes.append({
            "id": f"scene-{group[0]['start_ms']}", "title": f"场景 {len(scenes) + 1}",
            "start_ms": group[0]["start_ms"], "end_ms": group[-1]["end_ms"],
            "summary": summary or "此段暂无可引用的证据。", "chunk_ids": [c["id"] for c in group],
            "evidence_ids": [row["id"] for row in quotes],
        })
    return chunks, scenes


def append_rows(evidence, track, rows, duration):
    previous = next((row for row in reversed(evidence) if row["track"] == track), None)
    for n, (start, end, text, source) in enumerate(rows):
        start, end = max(0, start), min(duration, end)
        if start >= end or not text:
            continue
        if (previous and "OCR" in source and previous["source"] == source
                and previous["text"] == text and previous["end_ms"] == start):
            previous["end_ms"] = end
            continue
        previous = {"id": f"{track}-{start}ms-{n}", "track": track, "start_ms": start,
                    "end_ms": end, "text": text, "source": source}
        evidence.append(previous)


def build_context(path, work_dir, previous, publish):
    path, work_dir = Path(path), Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    info = probe(path)
    duration = info["duration_ms"]
    context = {**previous, "status": "processing", "duration_ms": duration}
    evidence = context.setdefault("evidence", [])
    tracks = context.setdefault("tracks", {})
    # Keep the published timeline usable while rebuilding only its visual evidence.
    refresh = tracks.get("visual", {}).get("refresh")
    selected = ["visual"] if refresh is not None else list(TRACKS)
    for i, track in enumerate(selected):
        label, extractor = TRACKS[track]
        state = refresh if refresh is not None else tracks.setdefault(track, {})
        rows_for_track = state["rows"] if refresh is not None else evidence
        if state.get("status") in {"ready", "empty"}:
            continue
        start = state.get("done_ms", 0)
        state.update(status="processing", message=f"正在提取{label}。")
        share = 90 / len(selected)
        context.update(stage=f"提取{label}", progress=round((i + start / duration) * share))
        publish(context)
        try:
            with TemporaryDirectory(dir=work_dir) as temp:
                for end, rows in extractor(path, Path(temp), info, start):
                    append_rows(rows_for_track, track, rows, duration)
                    state.update(done_ms=end, progress=round(100 * end / duration))
                    context["progress"] = round((i + end / duration) * share)
                    publish(context)
            count = sum(row["track"] == track for row in rows_for_track)
            state.update(status="ready" if count else "empty", progress=100, done_ms=duration,
                         message=f"{count} 条{label}证据" if count else f"未检出{label}。")
        except InterruptedError:
            raise
        except Unavailable as exc:
            state.update(status="unavailable", message=str(exc))
        except Exception as exc:
            state.update(status="failed", message=f"{label}提取失败：{exc}")
        if refresh is not None:
            del tracks[track]["refresh"]
            if state["status"] not in {"ready", "empty"}:
                context.update(status="partial", error=state["message"], progress=100)
                return context
            tracks[track] = {k: v for k, v in state.items() if k != "rows"}
            evidence[:] = [row for row in evidence if row["track"] != track] + rows_for_track
            context.update(chunks=[], scenes=[], index={"status": "pending"})
        publish(context)

    context.update(stage="整理场景与检索块", progress=92)
    evidence.sort(key=lambda row: (row["start_ms"], row["end_ms"], row["id"]))
    context["chunks"], context["scenes"] = timeline(duration, evidence)
    publish(context)
    from .indexing import build_index
    context.update(stage="构建向量索引", progress=95)
    publish(context)
    try:
        context["index"] = build_index(path, work_dir, context)
    except Exception as exc:
        context["index"] = {"status": "failed", "message": f"向量索引失败：{exc}"}
    all_tracks = all(tracks.get(track, {}).get("status") in {"ready", "empty"} for track in TRACKS)
    complete = all_tracks and context["index"].get("status") == "ready"
    context.update(status="ready" if complete else "partial", stage="处理完成", progress=100)
    return context
