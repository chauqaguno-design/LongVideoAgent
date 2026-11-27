"""Group corroborating observations using both temporal continuity and source location."""

from collections import defaultdict
from hashlib import sha256
import json
import unicodedata


def normalized(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def speech_flags(span, start, end, prior):
    """Separate duplicate chunk output from the same words spoken at a different time."""
    key = normalized(span["text"])
    occurrences = prior.setdefault(key, [])
    if any(min(end, b) - max(start, a) >= 0.7 * min(end - start, b - a) for a, b in occurrences):
        return None
    flags = []
    if not span.get("words"):
        flags.append("SEGMENT_LEVEL_TIMING")
    if occurrences:
        flags.append("REPEATED_TEXT")
    if (span["no_speech_prob"] > 0.6 and span["avg_logprob"] < -1) or span.get(
        "compression_ratio", 0
    ) > 2.4:
        flags.append("LOW_SPEECH_CONFIDENCE")
    occurrences.append((start, end))
    return flags


def bounds(points):
    xs, ys = zip(*points)
    return min(xs), min(ys), max(xs), max(ys)


def same_region(a, b):
    x1, y1, x2, y2 = a
    u1, v1, u2, v2 = b
    overlap = max(0, min(x2, u2) - max(x1, u1)) * max(0, min(y2, v2) - max(y1, v1))
    outer = max((x2 - x1) * (y2 - y1), (u2 - u1) * (v2 - v1))
    return outer > 0 and overlap / outer >= 0.7


def ocr_occurrences(frames, max_gap_us=1_500_000):
    """A blank intervening frame or time gap starts a new occurrence."""
    groups = []
    previous = {}
    for frame_no, (timestamp, lines) in enumerate(frames):
        current = defaultdict(list)
        for line in lines:
            text = normalized(line["text"])
            box = bounds(line["polygon"])
            candidates = previous.get(text, [])
            group = next(
                (
                    g
                    for g in candidates
                    if timestamp - g["end_us"] <= max_gap_us and same_region(g["box"], box)
                ),
                None,
            )
            if group is None:
                group = dict(
                    text=line["text"],
                    start_us=timestamp,
                    end_us=timestamp,
                    box=box,
                    first_box=box,
                    frames=[],
                )
                groups.append(group)
            else:
                candidates.remove(group)
                group["end_us"] = timestamp
                group["box"] = box
            group["frames"].append((frame_no, line["confidence"]))
            current[text].append(group)
        previous = current
    return groups


def viewing_notes(duration, rows, segment_ranges):
    """Build notes only from child evidence. Changes affect the groups containing that evidence."""
    rows = sorted(rows, key=lambda r: (r["range"]["start_us"], r["evidence_id"]))
    by_evidence, events = {}, []
    last_seen = {}
    for row in rows:
        if row["source"] == "sampled-frame":
            continue
        # A repeated description at a later time can describe a second, distinct occurrence.
        key = (row.get("media_revision_id"), row["source"], normalized(row["claim"]), row["support"])
        start, end = row["range"]["start_us"], row["range"]["end_us"]
        event = last_seen.get(key)
        if event is None or start >= event["end_us"]:
            identity = [*key, start]
            event = dict(
                event_id=sha256(json.dumps(identity, ensure_ascii=False).encode()).hexdigest(),
                start_us=start,
                end_us=end,
                text=row["claim"],
                source=row["source"],
                support=row["support"],
                evidence_ids=[],
            )
            last_seen[key] = event
            events.append(event)
        event["end_us"] = max(event["end_us"], end)
        event["evidence_ids"].append(row["evidence_id"])
        by_evidence[row["evidence_id"]] = event

    def group(start, end, children):
        refs = [r["evidence_id"] for r in children]
        content = [(r["evidence_id"], r["claim"], r["source"], r["support"], r["range"]) for r in children]
        dependency = sha256(json.dumps(content, ensure_ascii=False).encode()).hexdigest()
        statements = [
            dict(
                text=r["claim"],
                evidence_id=r["evidence_id"],
                range=r["range"],
                source=r["source"],
                support=r["support"],
            )
            for r in children
            if r["source"] != "sampled-frame"
        ]
        event_ids = list(dict.fromkeys(by_evidence[e]["event_id"] for e in refs if e in by_evidence))
        return dict(
            start_us=start,
            end_us=end,
            evidence_ids=refs,
            event_ids=event_ids,
            claims=statements,
            dependency_sha256=dependency,
        )

    def groups(ranges):
        result = []
        active = []
        pos = 0
        for start, end in ranges:
            active = [r for r in active if r["range"]["end_us"] > start]
            while pos < len(rows) and rows[pos]["range"]["start_us"] < end:
                if rows[pos]["range"]["end_us"] > start:
                    active.append(rows[pos])
                pos += 1
            result.append(group(start, end, active))
        return result

    sections = groups((s, min(s + 300_000_000, duration)) for s in range(0, duration, 300_000_000))
    # Begin, middle and end of the evidence in each chapter, capped across the whole film.
    highlights = []
    for section in sections:
        claims = section["claims"]
        if claims:
            highlights.extend(claims[i] for i in sorted({0, len(claims) // 2, len(claims) - 1}))
    if len(highlights) > 8:
        highlights = [highlights[round(i * (len(highlights) - 1) / 7)] for i in range(8)]
    return dict(
        version="evidence-notes-v2",
        duration_us=duration,
        overview=dict(
            chapter_count=len(sections),
            highlights=highlights,
            dependency_sha256=sha256(
                "".join(s["dependency_sha256"] for s in sections).encode()
            ).hexdigest(),
        ),
        events=events,
        chapters=sections,
        segments=groups(segment_ranges),
        coverage_note="画面仅在指定采样点观察；每条笔记均可回到原片或对应来源核对。",
    )
