"""Interpret unambiguous timecodes and timeline commands without a model."""

from decimal import Decimal
import re
from .retrieval_documents import normalize_text
from .render_formats import EXPORT_FORMATS
from .workflow_routing import QUOTES
from .workflow_state import NeedsInput, stable_id
from .spec_contracts import validate_contract


PROFILE = dict(width=1280, height=720, fps_num=25, fps_den=1, audio_sample_rate=48000)
CLOCK = r"(?:\d+:){0,2}\d+(?:\.\d{1,6})?"
INTERVAL = re.compile(
    rf"(?<![\d.:])(?:第\s*)?({CLOCK})\s*(?:秒|s)?\s*(?:至|到|[-–—~～])\s*(?:第\s*)?({CLOCK})\s*(?:秒|s)?",
    re.I,
)
QUOTE = re.compile(r'[「“"‘《](?P<literal>[^」”"’》]{1,200})[」”"’》]')
KEEP_AUDIO = r"保留原[声音]|保留声音|保持原声|保留音轨|带原声|不要静音|保留讲话|保留对白"
AUDIO = KEEP_AUDIO + r"|静音|去掉原声|去掉声音|不要原声|移除音轨"
TEXT_CONDITION = re.compile(
    r"(?:(?P<subject>字幕|台词|对白)\s*)?(?P<verb>必须包含|不得包含|不包含|包含|包括|排除)\s*"
    r"(?:(?P<object>字幕|台词|对白)\s*)?[：:]?\s*" + QUOTE.pattern
)
EXCLUDE_VERBS = {"排除", "不包含", "不得包含"}
TARGET = re.compile(r"(?:总时长|目标时长)\s*(?:为|是)?\s*(\d+(?:\.\d{1,6})?)\s*秒")

# Keep quoted dialogue intact, including a line such as “导出为 MKV”.
_FORMAT_LIST = r"[a-z][a-z0-9]*(?:\s*[/、或和]\s*[a-z][a-z0-9]*)*"
_EXPORT_FORMAT = re.compile(
    QUOTES.pattern
    + rf"|(?:导出|输出|保存|生成)(?:为|成)?\s*\.?(?P<after>{_FORMAT_LIST})(?:\s*格式)?"
      rf"|(?:以|用)?\.?(?P<before>{_FORMAT_LIST})\s*(?:格式)?导出",
    re.IGNORECASE,
)


def export_instruction(text, reply=None):
    """Return the edit instruction and its container. Explicit formats never silently fall back to MP4."""
    requested = set()

    def replace(match):
        formats = match["after"] or match["before"]
        if not formats:
            return match[0]
        requested.update(re.findall(r"[a-z][a-z0-9]*", formats.lower()))
        return "导出"

    instruction = _EXPORT_FORMAT.sub(replace, text)
    choice = (reply or "").strip().lower()
    if choice in EXPORT_FORMATS:
        return instruction, choice
    if not requested:
        return instruction, "mp4"
    if len(requested) == 1 and requested.issubset(EXPORT_FORMATS):
        return instruction, requested.pop()
    options = [name.upper() for name in EXPORT_FORMATS]
    raise NeedsInput("每次导出一个文件。请选择本次使用的格式：" + "、".join(options) + "。", options)


def clarified_instruction(original, reply):
    if not reply:
        return original
    if reply.strip().lower() in EXPORT_FORMATS:
        return original
    if re.fullmatch(
        r"\s*第\s*[零一二两三四五六七八九十\d]+\s*(?:个|段|条|项)(?:候选|结果)?[。！]?\s*", reply
    ):
        return original
    if re.fullmatch(AUDIO, reply.strip()):
        parts = re.split(r'([「“"‘《][^」”"’》]+[」”"’》])', original)
        return "".join(p if i % 2 else re.sub(AUDIO, "", p) for i, p in enumerate(parts)) + " " + reply
    return reply


def micros(value):
    secs = Decimal(0)
    for part in value.split(":"):
        secs = secs * 60 + Decimal(part)
    return int(secs * 1_000_000)


_DIGITS = "零一二三四五六七八九"
_CN_NUMBER = r"[一二两三四五六七八九]?十[一二三四五六七八九]?|[一二两三四五六七八九]"
_SECONDS = rf"(\d+(?:\.\d{{1,6}})?|{_CN_NUMBER})\s*秒"
_RELATIVE_FILLER = re.compile(
    AUDIO + r"|最后一段|末段|片尾|片头|请|帮我|把|将|从|刚才|刚刚|上一次|上个|上一|这个|那个|这一|那一|这|那"
    r"|已选|选中的?|候选|结果|当前|原片|片段|素材|时间线|成片|版本|版|剪辑|视频|一段|一份|段|个|条"
    r"|要|也|先|再|多|且|并|然后|同时|直接|开始|结束|的|总长|总时长|时长|剪出来|剪出|剪下|剪成|剪"
    r"|截取|预览|导出|输出|保存|生成|[\s，。、,;；!！]"
)
REPLACE_LAST = re.compile(r"^\s*(?:请)?(?:帮我)?(?:把|将)?(?:最后一段|末段)(?:换成|换为|替换成|替换为)")


def phrase_number(value):
    if value[0].isascii():
        return Decimal(value)
    value = value.replace("两", "二")
    if "十" in value:
        tens, units = value.split("十")
        return (_DIGITS.index(tens) if tens else 1) * 10 + (_DIGITS.index(units) if units else 0)
    return _DIGITS.index(value)


def referenced_edit(text, *, candidate):
    """Relative instructions apply only to a server-bound candidate or an existing timeline."""
    rest = text
    options = {}
    patterns = {
        "before_us": (
            r"(?:往前|向前|提前|前面|前边|前)(?:再|多|延伸|延长|保留|留|加上|加|扩)*\s*" + _SECONDS,
            _SECONDS + "前(?:面)?开始",
        ),
        "after_us": (
            r"(?:往后|向后|延后|后面|后边|后)(?:再|多|延伸|延长|保留|留|加上|加|扩)*\s*" + _SECONDS,
        ),
        "max_us": (
            r"(?:最长不超过|最多|至多|不超过|不要超过|控制在|限制在|最长|上限(?:为)?)\s*"
            + _SECONDS
            + r"(?:以[内下]|之内|内)?",
            _SECONDS + r"(?:以内|之内)",
        ),
    }
    for name, variants in patterns.items():
        for pattern in variants:
            match = re.search(pattern, rest)
            if match:
                if name in options:
                    raise NeedsInput("请为同一处剪辑调整只指定一个时长。")
                options[name] = int(phrase_number(match[1]) * 1_000_000)
                rest = rest[: match.start()] + " " + rest[match.end() :]
    keep = re.search(KEEP_AUDIO, rest)
    muted = re.search(r"静音|去掉原声|去掉声音|不要原声|移除音轨", rest.replace("不要静音", ""))
    if keep and muted:
        raise NeedsInput("请选定保留原声或静音。", kind="CONSTRAINT_CONFLICT")
    if keep or muted:
        options["audio"] = "MUTE" if muted else "KEEP_SOURCE"
    ordinal = re.search(rf"第\s*(\d+|{_CN_NUMBER})\s*(?:个|段|条|项)", rest)
    if ordinal:
        if not candidate:
            options["clip_number"] = int(phrase_number(ordinal[1]))
        rest = rest[: ordinal.start()] + rest[ordinal.end() :]
    elif any(mark in rest for mark in ("最后一段", "末段", "片尾")):
        options["last"] = True
    elif "片头" in rest:
        options["clip_number"] = 1
    if _RELATIVE_FILLER.sub("", rest):
        return None
    if not candidate and not options:
        return {"command": "render_current"} if requests_export(text) or "预览" in text else None
    if not options and not re.search(r"剪|截|导出|预览", text):
        return None
    return dict(command="candidate" if candidate else "adjust_clip", **options)


def relative_window(span, duration, options):
    start = max(0, span["start_us"] - options.get("before_us", 0))
    end = min(duration, span["end_us"] + options.get("after_us", 0))
    if "max_us" in options:
        end = min(end, start + options["max_us"])
    if end <= span["start_us"] or end <= start:
        raise NeedsInput(
            "时长上限会把选中的依据完全裁掉，请减少前留时间或提高上限。", kind="CONSTRAINT_CONFLICT"
        )
    return dict(start_us=start, end_us=end)


def literal_plan(text):
    """Interpret explicit time ranges, quoted text constraints and timeline edits locally."""
    replacement = re.fullmatch(r"\s*(?:请)?(?:把|将)?最后一段(?:换成|换为|替换成|替换为)\s*(.+)", text)
    if replacement:
        selected = clip_instruction(replacement[1])
        if selected and selected["command"] in ("quote", "ranges"):
            return dict(selected, replace_last=True)
        return None
    return clip_instruction(text)


def constrained_quote(text, audio):
    """Match a literal text brief only if every remaining clause is understood."""
    terms = list(TEXT_CONDITION.finditer(text))
    if not terms:
        return None
    rest = TEXT_CONDITION.sub("", text)
    targets = TARGET.findall(rest)
    if not targets and all(term["verb"] not in EXCLUDE_VERBS for term in terms):
        return None
    remainder = re.sub(
        AUDIO + r"|请|帮我|剪出|剪成|片段|视频|并且|并|同时|导出|[\s、，,。；;！!]", "", TARGET.sub("", rest)
    )
    if remainder or len(targets) > 1:
        return None
    positive = [m for m in terms if m["verb"] not in EXCLUDE_VERBS]
    if len(positive) != 1:
        raise NeedsInput("请给出一句必须出现的字幕或台词原文，再列出需排除的字幕原文。")
    include = positive[0]
    domain = include["subject"] or include["object"]
    exclusions = []
    for term in terms:
        if term["verb"] in EXCLUDE_VERBS:
            if (term["subject"] or term["object"] or domain) != "字幕":
                raise NeedsInput(
                    "可以逐条核对内嵌字幕。若要排除声音或画面，请指定已核实的区间，或明确改为排除字幕原文。"
                )
            exclusions.append(term["literal"])
    if domain == "字幕" and any(
        normalize_text(term) in normalize_text(include["literal"]) for term in exclusions
    ):
        raise NeedsInput(
            "必须保留的原文包含了要求排除的字幕原文，请调整其中一项。", kind="CONSTRAINT_CONFLICT"
        )
    target = micros(targets[0]) if targets else None
    if target == 0:
        raise NeedsInput("请给出大于零的目标时长。", kind="CONSTRAINT_CONFLICT")
    return dict(
        command="quote",
        query=include["literal"],
        first=False,
        audio=audio,
        must_exclude=exclusions,
        text_source=domain or "台词",
        duration_target_us=target,
        require_audio=bool(re.search(r"保留讲话|保留对白", rest)),
    )


def clip_instruction(text):
    command = QUOTE.sub("", text)
    keep = bool(re.search(KEEP_AUDIO, command))
    mute = bool(re.search(r"静音|去掉原声|去掉声音|不要原声|移除音轨", command.replace("不要静音", "")))
    if keep and mute:
        raise NeedsInput(
            "指令同时要求保留原声和静音，请选定一种声音策略。", ["保留原声", "静音"], "CONSTRAINT_CONFLICT"
        )
    audio = "MUTE" if mute else "KEEP_SOURCE"
    policies = AUDIO + r"|(?:不必|无需|不要|不用|不)?导出|同时|并且|并"
    if match := re.fullmatch(r"\s*(?:恢复|回到|还原)\s*(?:版本|v)\s*(\d+)\s*(?:版)?[。！]?\s*", text, re.I):
        return dict(command="restore", number=int(match[1]))
    if re.fullmatch(r"\s*(?:去掉|删除|删掉|移除)\s*(?:上一段|最后一段|末段)[。！]?\s*", text):
        return {"command": "remove_last"}
    if re.fullmatch(r"\s*(?:倒序|反转顺序|交换两段(?:的)?顺序|交换顺序)[。！]?\s*", text):
        return {"command": "reverse"}
    constrained = constrained_quote(text, audio)
    if constrained:
        return constrained
    matches = list(INTERVAL.finditer(text))
    if matches:
        rest = re.sub(policies, "", INTERVAL.sub("", text))
        rest = re.sub(
            r"保持原声|保留原声|静音|请|帮我|把|从|将|以及|和|再|然后|依次|按顺序|原片|视频|片段|两段|这段|截取|剪出来|剪出|剪辑|剪成|剪下|剪|拼接|连接|合并|在一起|生成|导出|预览|到|与|加上|添加|的|里|中|[、，,。.!！；;\s]+",
            "",
            rest,
        )
        if not rest:
            return dict(
                command="ranges",
                ranges=[dict(start_us=micros(m[1]), end_us=micros(m[2])) for m in matches],
                audio=audio,
            )
    match = QUOTE.search(text)
    if match and re.fullmatch(
        r"\s*(?:请|帮我|把|剪出来|剪出|剪下|找到|找出|再|然后|截取|台词|说|包含|有|了|的|片段|这句|一句|对应|那段|视频|画面|出现|所有|全部|第一次|首次|所说|的地方|中|里|和|[。！。，\s])*",
        re.sub(policies, "", text[: match.start()] + text[match.end() :]),
    ):
        return dict(command="quote", query=match[1], first="首次" in text or "第一次" in text, audio=audio)
    return None


def requests_export(text):
    instruction = QUOTE.sub("", text)
    return "导出" in instruction and not re.search(r"(?:不必|无需|不要|不用|不)\s*导出", instruction)


def brief_for(run, plan, profile):
    cp = run.checkpoint
    brief = dict(
        brief_id=stable_id(run.id, f"brief:{cp['turn']}"),
        project_id=run.project_id,
        goal=clarified_instruction(cp["message"], cp.get("clarification")),
        source_media_revision_ids=cp["scope"]["media_revision_ids"],
        selection_mode="SEMANTIC" if plan["command"] in ("quote", "semantic") else "EXPLICIT_RANGES",
        duration_target_us=plan.get("duration_target_us"),
        duration_tolerance_us=plan.get("duration_tolerance_us", 0),
        order_policy=plan.get("order_policy", "USER_ORDER"),
        audio_policy=plan.get("audio", "KEEP_SOURCE"),
        must_include=plan.get("must_include", ([plan["query"]] if "query" in plan else [])),
        must_exclude=plan.get("must_exclude", []),
        output_profile=profile,
    )
    validate_contract("EditBrief", brief)
    return brief


def append_ranges(brief, ranges):
    if not ranges:
        raise NeedsInput("没有符合条件且经过核实的片段，可输入原片的起止秒数。")
    ops = []
    seen = set()
    total = 0
    for mid, span, proof in ranges:
        a, b = span["start_us"], span["end_us"]
        if a >= b:
            raise NeedsInput("每段的出点需要晚于入点，请重新给出区间。")
        if (mid, a, b) in seen:
            continue
        seen.add((mid, a, b))
        total += b - a
        clip = dict(
            media_revision_id=mid,
            source_start_us=a,
            source_end_us=b,
            evidence_ids=proof,
            audio_policy="mute" if brief["audio_policy"] == "MUTE" else "keep",
        )
        ops.append(dict(op="append_clip", clip=clip))
    target = brief["duration_target_us"]
    if target is not None and abs(total - target) > brief["duration_tolerance_us"]:
        raise NeedsInput(
            f"可核实的片段共 {total/1e6:g} 秒，与目标时长不符，请调整条件。", kind="CONSTRAINT_CONFLICT"
        )
    return ops


def exclude_subtitles(hits, words, rows, coverage):
    """A zero-match subtitle scan proves absence only within its covered source ranges."""
    needles = [normalize_text(word) for word in words]
    blocked = {}
    for row in rows:
        if row["source"] == "embedded-subtitle" and any(
            word in normalize_text(row["claim"]) for word in needles
        ):
            blocked.setdefault(row["media_revision_id"], []).append(row["range"])
    keep = []
    covered_hits = 0
    for hit in hits:
        mid, span = hit["media_revision_id"], hit["range"]
        start, end = span["start_us"], span["end_us"]
        if not any(c["start_us"] <= start and end <= c["end_us"] for c in coverage.get(mid, [])):
            continue
        covered_hits += 1
        if not any(start < cue["end_us"] and cue["start_us"] < end for cue in blocked.get(mid, [])):
            keep.append(hit)
    if hits and not keep:
        if covered_hits < len(hits):
            raise NeedsInput("部分候选区间缺少可完整核对的内嵌字幕，无法确认排除条件。请提供字幕或指定区间。")
        raise NeedsInput(
            "匹配的片段均与要求排除的字幕重叠，请修改包含或排除条件。", kind="CONSTRAINT_CONFLICT"
        )
    return keep


def choose_intervals(brief, intervals):
    """Keep one source-ordered selection per reachable duration."""
    target = brief["duration_target_us"]
    if target is None:
        return intervals
    tolerance = brief["duration_tolerance_us"]
    choices = {0: ()}
    seen = set()
    for i, (mid, span, _) in enumerate(intervals):
        key = (mid, span["start_us"], span["end_us"])
        if key in seen:
            continue
        seen.add(key)
        duration = span["end_us"] - span["start_us"]
        for total, picked in list(choices.items()):
            new = total + duration
            if new > target + tolerance or len(picked) >= 200:
                continue
            if new in choices and len(choices[new]) <= len(picked) + 1:
                continue
            picks = picked + (i,)
            if new == target:
                return [intervals[j] for j in picks]
            choices[new] = picks
            if len(choices) > 10000:
                raise NeedsInput("满足时长的组合过多，请缩小素材范围或指定要保留的某句字幕。")
    choices = {n: picked for n, picked in choices.items() if picked and abs(n - target) <= tolerance}
    if not choices:
        raise NeedsInput(
            f"保留整段原文时，候选无法拼出目标 {target/1e6:g} 秒，请调整时长或条件。",
            kind="CONSTRAINT_CONFLICT",
        )
    closest = min(choices, key=lambda n: abs(n - target))
    return [intervals[j] for j in choices[closest]]
