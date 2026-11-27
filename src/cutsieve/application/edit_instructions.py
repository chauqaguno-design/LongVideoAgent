"""Parse explicit cuts and build a brief from grounded source selections."""

from decimal import Decimal
import re

from .instruction_text import (
    INTERVAL, LAST, ORDINAL, QUOTES, RELATIVE_TRIMS, clauses, number, quoted_literal,
    replacement_request, timeline_operation,
)
from .render_formats import EXPORT, EXPORT_FORMATS, PREVIEW
from .sparse_retrieval import normalize_text
from .workflow_identity import generation_key
from .workflow_state import NeedsInput, stable_id


PROFILE = dict(width=1280, height=720, fps_num=25, fps_den=1, audio_sample_rate=48000)
KEEP_AUDIO = r"保留(?:原声|原音|声音|讲话|对白|音轨)|保持原声|带原声|不要静音"
MUTE_AUDIO = r"(?<!不要)静音|(?:去掉|不要)(?:原声|声音)|移除音轨"
AUDIO = KEEP_AUDIO + "|" + MUTE_AUDIO
TARGET = re.compile(r"(?:总时长|目标时长)\s*(?:为|是)?\s*(\d+(?:\.\d{1,6})?)\s*秒")
TERM = re.compile(
    r"(?:(?P<subject>字幕|台词|对白)\s*)?(?P<verb>必须包含|不得包含|不包含|包含|包括|排除)\s*"
    rf"(?:(?P<object>字幕|台词|对白)\s*)?[：:]?\s*(?P<literal>{QUOTES.pattern})|{QUOTES.pattern}"
)
EXCLUDE = {"排除", "不包含", "不得包含"}
PADDING = re.compile(
    rf"{AUDIO}|(?:不必|不用|无需|不要|不)?(?:导出|预览)|请|帮我|把|将|从|然后|再|并且|并|同时|"
    r"原片|视频|这段|那段|两段|片段|剪出来|剪出|剪成|剪下|剪辑|剪|截取|依次|按顺序|拼接|拼起来|合并|"
    r"和|以及|与|的|里|中|[、，,。.!！；;\s]+"
)
DIALOGUE_WORDS = re.compile(r"台词|对白|字幕|找到|出现|说了?|包含|有|对应|这句|一句|首次|第一次|所有|全部")
REFERENCE_WORDS = re.compile(
    rf"{LAST}|片头|片尾|刚才|刚刚|上[一]?版|这个|那个|这|那|当前|候选|结果|时间线|成片|"
    r"直接|开始|结束|一段|一份|段|个|条|总时长|时长"
)
PRESERVE_REST = re.compile(r"(?:其他|其余)(?:片段|部分)?(?:都)?(?:保持不变|不变|不动|保留原样)")


def clarified_instruction(original, reply):
    if not reply or reply.strip().lower() in EXPORT_FORMATS or re.fullmatch(
        rf"{ORDINAL}(?:候选|结果)?[。！]?", reply.strip()
    ):
        return original
    if re.fullmatch(AUDIO, reply.strip()):
        pattern = re.compile(QUOTES.pattern + rf"|(?P<audio>{AUDIO})")
        return pattern.sub(lambda m: "" if m["audio"] else m[0], original) + " " + reply
    return reply


def micros(value):
    seconds = Decimal(0)
    for component in value.split(":"):
        seconds = seconds * 60 + Decimal(component)
    return int(seconds * 1_000_000)


def requested_audio(text):
    text = QUOTES.sub("", text)
    keep, mute = re.search(KEEP_AUDIO, text), re.search(MUTE_AUDIO, text)
    if keep and mute:
        raise NeedsInput("指令同时要求保留原声和静音，请选定一种声音策略。",
                         ["保留原声", "静音"], "CONSTRAINT_CONFLICT")
    return "MUTE" if mute else "KEEP_SOURCE" if keep else None


def referenced_edit(text, *, candidate):
    """Apply relative trims to a bound candidate or timeline clip."""
    rest = text
    options = {}
    for key, pattern in RELATIVE_TRIMS:
        matches = pattern.findall(rest)
        if matches:
            if key in options or len(matches) > 1:
                raise NeedsInput("请为同一处剪辑调整只指定一个时长。")
            options[key] = int(number(matches[0]) * 1_000_000)
            rest = pattern.sub(" ", rest)
    if audio := requested_audio(rest):
        options["audio"] = audio
    if match := re.search(ORDINAL, rest):
        if not candidate:
            options["clip_number"] = int(number(match[1]))
        rest = rest[:match.start()] + rest[match.end():]
    elif re.search(LAST + "|片尾", rest):
        options["last"] = True
    elif "片头" in rest:
        options["clip_number"] = 1
    if PADDING.sub("", REFERENCE_WORDS.sub("", rest)):
        return None
    rendering = any(EXPORT.search(p[0]) or PREVIEW.search(p[0]) for p in clauses(text))
    if not candidate and not options:
        return dict(command="render_current") if rendering else None
    if options or rendering or re.search(r"剪|截取", text):
        return dict(command="candidate" if candidate else "adjust_clip", **options)
    return None


def relative_window(span, duration, options):
    start = max(0, span["start_us"] - options.get("before_us", 0))
    end = min(duration, span["end_us"] + options.get("after_us", 0), start + options.get("max_us", duration))
    if end <= max(start, span["start_us"]):
        raise NeedsInput("时长上限会把选中的依据完全裁掉，请减少前留时间或提高上限。", kind="CONSTRAINT_CONFLICT")
    return dict(start_us=start, end_us=end)


def _literal_constraints(terms, text, audio):
    include = [m for m in terms if m["verb"] not in EXCLUDE]
    if len(include) != 1:
        raise NeedsInput("请给出一句必须出现的字幕或台词原文，再列出需排除的字幕原文。")
    match = include[0]
    literal = quoted_literal(match["literal"])
    source = match["subject"] or match["object"] or "台词"
    excluded = []
    for term in (m for m in terms if m["verb"] in EXCLUDE):
        if (term["subject"] or term["object"] or source) != "字幕":
            raise NeedsInput("可以逐条核对内嵌字幕。若要排除声音或画面，请指定已核实的区间，或明确改为排除字幕原文。")
        excluded.append(quoted_literal(term["literal"]))
    if not literal or not all(excluded) or any(len(term) > 200 for term in [literal, *excluded]):
        return None
    if source == "字幕" and any(normalize_text(term) in normalize_text(literal) for term in excluded):
        raise NeedsInput("必须保留的原文包含了要求排除的字幕原文，请调整其中一项。", kind="CONSTRAINT_CONFLICT")
    return dict(command="quote", query=literal, first=False, audio=audio, must_exclude=excluded,
                text_source=source, require_audio="保留讲话" in text or "保留对白" in text)


def literal_plan(text):
    """Return a local plan when every clause of the edit has explicit syntax."""
    if replacement := replacement_request(text):
        text = PRESERVE_REST.sub("", replacement[1])
    if operation := timeline_operation(text):
        return operation
    audio = requested_audio(text) or "KEEP_SOURCE"
    terms = [m for m in TERM.finditer(text) if m["verb"]]
    rest = TERM.sub(lambda m: "" if m["verb"] else "〈引用〉", text)
    targets = TARGET.findall(rest)
    if len(targets) > 1:
        return None
    rest = TARGET.sub("", rest)
    intervals = list(INTERVAL.finditer(rest))
    quotes = list(QUOTES.finditer(text))
    plan = None
    if terms and not PADDING.sub("", rest):
        plan = _literal_constraints(terms, text, audio)
    elif intervals and not PADDING.sub("", INTERVAL.sub("", rest)):
        plan = dict(command="ranges", audio=audio,
                    ranges=[dict(start_us=micros(m[1]), end_us=micros(m[2])) for m in intervals])
    elif len(quotes) == 1 and PADDING.sub("", DIALOGUE_WORDS.sub("", rest)) == "〈引用〉":
        literal = quoted_literal(quotes[0][0])
        if 0 < len(literal) <= 200:
            plan = dict(command="quote", query=literal, first=bool(re.search(r"首次|第一次", rest)), audio=audio)
    if plan and targets:
        target = micros(targets[0])
        if not target:
            raise NeedsInput("请给出大于零的目标时长。", kind="CONSTRAINT_CONFLICT")
        plan["duration_target_us"] = target
    return plan


def brief_for(run, plan, profile):
    cp = run.checkpoint
    target = plan.get("duration_target_us")
    return dict(
        brief_id=stable_id(run.id, generation_key(cp, f"brief:{cp.attempt.turn}")),
        project_id=run.project_id,
        goal=clarified_instruction(cp.spec.message, cp.attempt.clarification),
        source_media_revision_ids=list(cp.spec.scope.media_revision_ids),
        selection_mode="SEMANTIC" if plan["command"] in ("quote", "semantic") else "EXPLICIT_RANGES",
        duration_target_us=target,
        duration_tolerance_us=plan.get("duration_tolerance_us", 2_000_000 if target is not None else 0),
        order_policy=plan.get("order_policy", "USER_ORDER"),
        audio_policy=plan.get("audio", "KEEP_SOURCE"),
        must_include=plan.get("must_include", [plan["query"]] if "query" in plan else []),
        must_exclude=plan.get("must_exclude", []),
        output_profile=profile,
    )


def append_ranges(brief, ranges):
    if not ranges:
        raise NeedsInput("没有符合条件且经过核实的片段，可输入原片的起止秒数。")
    clips, total = {}, 0
    for mid, span, evidence in ranges:
        start, end = span["start_us"], span["end_us"]
        if start >= end:
            raise NeedsInput("每段的出点需要晚于入点，请重新给出区间。")
        if (mid, start, end) not in clips:
            clips[mid, start, end] = dict(media_revision_id=mid, source_start_us=start, source_end_us=end,
                                          evidence_ids=evidence, audio_policy="mute" if brief["audio_policy"] == "MUTE" else "keep")
            total += end - start
    target = brief["duration_target_us"]
    if target is not None and abs(total - target) > brief["duration_tolerance_us"]:
        raise NeedsInput(f"可核实的片段共 {total/1e6:g} 秒，与目标时长不符，请调整条件。", kind="CONSTRAINT_CONFLICT")
    return [dict(op="append_clip", clip=clip) for clip in clips.values()]


def choose_intervals(brief, intervals):
    """Choose the closest supported duration without splitting a source occurrence."""
    target = brief["duration_target_us"]
    if target is None:
        return intervals
    tolerance = brief["duration_tolerance_us"]
    choices, seen = {0: ()}, set()
    for i, (mid, span, _) in enumerate(intervals):
        start, end = span["start_us"], span["end_us"]
        if (mid, start, end) in seen:
            continue
        seen.add((mid, start, end))
        for total, picked in list(choices.items()):
            new = total + end - start
            if new > target + tolerance or len(picked) >= 200 or (new in choices and len(choices[new]) <= len(picked) + 1):
                continue
            picks = picked + (i,)
            if new == target:
                return [intervals[j] for j in picks]
            choices[new] = picks
            if len(choices) > 10000:
                raise NeedsInput("满足时长的组合过多，请缩小素材范围或指定要保留的某句字幕。")
    best = min((n for n in choices if n and abs(n - target) <= tolerance), key=lambda n: abs(n - target), default=None)
    if best is None:
        raise NeedsInput(f"保留整段原文时，候选无法拼出目标 {target/1e6:g} 秒，请调整时长或条件。",
                         kind="CONSTRAINT_CONFLICT")
    return [intervals[j] for j in choices[best]]
