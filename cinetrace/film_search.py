"""Find approximate scenes in one film and verify candidates against its source footage."""

from collections import Counter
from math import log
import re
import unicodedata

from . import film_media


def _normalize(text):
    return unicodedata.normalize("NFKC", text).casefold().strip()


def _tokens(text):
    tokens = set()
    for word in re.findall(r"[\u3400-\u9fff]+|[a-z0-9]+", _normalize(text)):
        if "\u3400" <= word[0] <= "\u9fff":
            tokens.update(c for c in word if c not in "的了呢吗是在和与有这那我")
            tokens.update(word[i:i + 2] for i in range(len(word) - 1))
        elif word not in {"a", "an", "at", "in", "of", "on", "the", "to", "and", "is", "was"}:
            tokens.add(word)
    return tokens


def _search_text(row):
    fields = [row["scene"], *row["people"], *row["objects"], *row["actions"], row["transcript"]]
    return _normalize("\n".join(fields))


def _rank(candidates, texts, tokens, frequency, phrases):
    wanted = set().union(*(_tokens(p) for p in phrases))
    scores = []
    for i in candidates:
        shared = wanted & tokens[i]
        if not shared:
            continue
        score = 0.0
        for token in shared:
            weight = 0.25 if len(token) == 1 and "\u3400" <= token <= "\u9fff" else 1.0
            score += weight * (1 + log((len(texts) + 1) / (frequency[token] + 1)))
        # Reward whole Chinese phrases and English word sequences, e.g. "car chase".
        for phrase in phrases:
            if len(phrase) > 1 and re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", texts[i]):
                score += 3
        scores.append((score, i))
    return [i for score, i in sorted(scores, key=lambda item: (-item[0], item[1]))]


def _with_neighbors(rows, i):
    """Include up to eight seconds from each contiguous neighbor to resolve missing context."""
    start, end = rows[i]["start_ms"], rows[i]["end_ms"]
    if i > 0 and rows[i - 1]["end_ms"] == start:
        start = max(rows[i - 1]["start_ms"], start - 8_000)
    if i + 1 < len(rows) and rows[i + 1]["start_ms"] == end:
        end = min(rows[i + 1]["end_ms"], end + 8_000)
    return start, end


def find_scene(path, query, records, background, models, *, budget=3, dialogue=None):
    """Spend at most budget visual verifications and budget text-only query expansions."""
    if not query.strip():
        raise ValueError("请输入要查找的片段。")
    if type(budget) is not int or budget < 0:
        raise ValueError("复核次数必须是非负整数。")

    rows = sorted(records, key=lambda row: row["start_ms"])
    texts = [_search_text(row) for row in rows]
    tokens = [_tokens(text) for text in texts]
    frequency = Counter(token for group in tokens for token in group)
    remaining = set(range(len(rows)))
    trace, feedback, partials = [], [], {}
    followup = None
    result = {"status": "no_match", "match": None, "trace": trace, "verification_calls": 0,
              "budget": budget, "stop_reason": "budget_exhausted"}
    for _ in range(budget):
        if followup is None:
            if not remaining:
                result["stop_reason"] = "candidates_exhausted"
                break
            suggestions = models.expand(query, background, feedback)
            terms = list(dict.fromkeys(_normalize(t) for t in [query, *suggestions] if t.strip()))
            trace.append({"step": "expand", "terms": terms})
            ranked = _rank(remaining, texts, tokens, frequency, terms)
            if not ranked:
                result["stop_reason"] = "no_index_hit"
                break
            i = ranked[0]
            remaining.remove(i)
            start, end = rows[i]["start_ms"], rows[i]["end_ms"]
            prior = None
        else:
            i, start, end, prior = followup
            followup = None

        if dialogue is None:
            # An older index only has whole-window transcripts.
            transcript = "\n".join(r["transcript"] for r in rows if r["transcript"]
                                   and start <= r["start_ms"] and r["end_ms"] <= end)
        else:
            transcript = "\n".join(r["text"] for r in dialogue if r["start_ms"] < end and r["end_ms"] > start)
        context = {"film": background, "window": {"start_ms": start, "end_ms": end},
                   "previous_observation": prior}
        clip = film_media.clip_for_window(path, start, end)
        check = models.verify(clip, query, transcript, context)
        status, reason, missing = check["status"], check["reason"], check["missing"]
        match = {"start_ms": start, "end_ms": end, "approximate": True, "reason": reason, "missing": missing}
        trace.append({"step": "verify", "status": status, "with_neighbors": prior is not None, **match})
        result["verification_calls"] += 1
        if status == "match":
            return {**result, "status": "verified", "match": match, "stop_reason": "verified"}
        feedback.append("；".join([reason, *missing]))
        if status == "partial":
            partials[i] = match
            wider = _with_neighbors(rows, i)
            if prior is None and wider != (start, end):
                followup = (i, *wider, check)
        elif status == "reject":
            partials.pop(i, None)
        else:
            raise ValueError(f"未知的片段复核结果：{status}")
    if partials:
        result.update(status="partial", match=min(partials.values(), key=lambda m: len(m["missing"])))
    return result
