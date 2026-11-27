"""Recognize explicit requests and defer ambiguous language to the budgeted router."""

from dataclasses import asdict, dataclass
import json
import re

from .instruction_text import (
    INTERVAL, QUOTES, RELATIVE_TRIMS, candidate_reference, clauses, replacement_request, timeline_operation,
)
from .render_formats import EXPORT, FORMAT_QUESTION, PREVIEW


@dataclass(frozen=True)
class RouteDecision:
    intent: str | None
    source: str
    reason: str
    question: str | None
    plan: tuple[str, ...]

    def as_dict(self):
        return dict(asdict(self), plan=list(self.plan))


EDIT = re.compile(
    r"剪(?:出|成|下|掉|一段|\s*(?:第|\d))|(?:请|帮我|给我)剪|裁剪|截取|拼接|拼起来|"
    r"删掉|删除|移除|去掉|换成|替换|重排|静音|(?:做|制作|生成).{0,16}(?:短片|短视频|集锦|预告片)"
)
READ = re.compile(r"^(?:请|帮我|帮忙|麻烦|给我|先|只)*(?:找|查找|查询|搜索|检索|定位|核对|核实|看看|解释|哪)")
ABOUT = re.compile(r"怎么|怎样|如何|为什么|什么意思|(?:导出|剪辑|预览)(?:的)?(?:按钮|步骤|设置)|了(?:哪|什么|谁)")
QUESTION = re.compile(r"在哪|何时|什么时候|哪.{0,2}(?:段|秒|镜头)|时间[戳点]?|几次|多少次|首次|第一次|说过|说了|解释|说明|总结|概括|概述|复述|讲解|什么|谁|是否|有没有")
PICTURE = re.compile(r"画面|镜头|场景|动作|开门|转身|挥手|在做啥|在干嘛|在做什么")
DIALOGUE = re.compile(r"〈引用〉|台词|对白|字幕|这句话|那句话|旁白")
VIDEO_GOAL = re.compile(r"(?:想要|想做|需要|给我|制作|生成).{0,24}(?:成片|短片|短视频|集锦|片花|预告)")
AMBIGUOUS = "任务目标不够明确"


def references_edit(message):
    text = " ".join(part[0] for part in clauses(message)).strip()
    if re.search(r"新建|另做|另剪|重新剪|从头剪", text):
        return False
    return bool(
        timeline_operation(text) or replacement_request(text)
        or re.search(r"上一?版|(?:刚才|当前|现有)的?(?:剪辑|成片|时间线)", text)
        or (candidate_reference(text) is None and any(p.search(text) for _, p in RELATIVE_TRIMS))
    )


def clarify(reason=AMBIGUOUS, question="你想定位片段、核对画面，还是生成一段剪辑？", source="RULE"):
    return RouteDecision(None, source, reason, question, ())


def _write_clause(text):
    if ABOUT.search(text):
        return ""
    if READ.search(text):
        parts = re.split(r"(?:然后|(?:核对|核实|确认)后|并且|并|再)(?=(?:帮我)?(?:把|剪|拼|删|导出|预览|截))", text, maxsplit=1)
        return parts[1] if len(parts) == 2 else ""
    return text


def _visual(text):
    check = re.search(r"复核|核实|核对|逐帧|检查.{0,14}(?:是否|是不是)", text)
    if DIALOGUE.search(text):
        return bool(check and PICTURE.search(text) or re.search(r"[时后].{0,18}(?:画面|表情|动作|反应)", text))
    return bool(check or PICTURE.search(text) or (READ.search(text) and "片段" in text))


def _editing(text):
    return EDIT.search(text) or timeline_operation(text) or any(p.search(text) for _, p in RELATIVE_TRIMS)


def _decision(intent, text, actions, *, has_edit, source="RULE"):
    if intent == "QUESTION":
        return RouteDecision(intent, source, "从素材证据定位或解释问题", None, ("SEARCH", "ANSWER"))
    if intent == "INSPECT":
        return RouteDecision(intent, source, "需要回看原片核对画面", None, ("SEARCH", "INSPECT", "ANSWER"))
    if intent != "EDIT":
        raise ValueError("Unsupported workflow intent")
    render = tuple(stage for stage, pattern in (("PREVIEW", PREVIEW), ("EXPORT", EXPORT)) if pattern.search(actions))
    selected = candidate_reference(text) is not None
    fixed = INTERVAL.search(text) or DIALOGUE.search(text) or selected
    existing = references_edit(text)
    if has_edit and render and not (fixed or _editing(actions)):
        return RouteDecision(intent, source, "使用现有剪辑版本", None, render)
    visual = _visual(text) or (not fixed and (not existing or replacement_request(text))
                               and not re.search(r"拼接|拼起来", actions))
    search = ("SEARCH", "INSPECT") if visual else ("SEARCH",) if selected or "〈引用〉" in text else ()
    plan = search + ("EDIT", "PREVIEW") + (("EXPORT",) if "EXPORT" in render else ())
    return RouteDecision(intent, source, "按用户要求组织片段并生成预览", None, plan)


def resolve_route(message, *, has_edit=False):
    parts = clauses(message)
    text = " ".join(part[0] for part in parts).strip()
    actions = " ".join(_write_clause(part[0].strip()) for part in parts).strip()
    denied = " ".join(part[1] for part in parts if len(part) == 2)
    edits = _editing(actions) or INTERVAL.fullmatch(actions)
    undo_cut = re.fullmatch(r"(?:剪(?:辑)?|编辑|修改)(?:了|吧)?", denied.strip())
    if any(p.search(actions) and p.search(denied) for p in (EXPORT, PREVIEW)) or (edits and (undo_cut or EDIT.search(denied))):
        return clarify("指令同时要求和禁止同一类操作", "这次要执行剪辑、预览或导出中的哪几项？")
    if FORMAT_QUESTION.search(QUOTES.sub("", message)) and not edits:
        return RouteDecision("QUESTION", "RULE", "查询工作台支持的成片格式", None, ("ANSWER",))
    existing = references_edit(text)
    rendering = EXPORT.search(actions) or PREVIEW.search(actions)
    if existing and not has_edit and (edits or rendering):
        return clarify("请求引用了现有剪辑", "你要继续修改哪个剪辑版本？")
    if edits or rendering:
        if not (edits or has_edit or INTERVAL.search(text) or DIALOGUE.search(text) or candidate_reference(text) is not None):
            return clarify("尚未指定要预览或导出的范围", "需要导出哪段原片或哪个剪辑版本？")
        return _decision("EDIT", text, actions, has_edit=has_edit)
    if _visual(text):
        return _decision("INSPECT", text, actions, has_edit=has_edit)
    if READ.search(text) or ABOUT.search(text) or QUESTION.search(text) or re.match(r"(?:台词|对白|字幕)\s*[:：]", text):
        return _decision("QUESTION", text, actions, has_edit=has_edit)
    return clarify("请求排除了部分操作") if denied else clarify()


def explicit_route(intent, message, *, has_edit=False):
    local = resolve_route(message, has_edit=has_edit)
    parts = clauses(message)
    if intent == "EDIT":
        if local.intent is None and local.reason != AMBIGUOUS:
            return RouteDecision(None, "USER", local.reason, local.question, ())
        if any(len(p) == 2 and "剪" in p[1] for p in parts):
            return clarify("请求排除了剪辑操作", source="USER")
    text = " ".join(part[0] for part in parts)
    return _decision(intent, text, " ".join(_write_clause(part[0]) for part in parts), has_edit=has_edit, source="USER")


def flash_route(message, *, has_edit=False, call, skill_catalog=None):
    local = resolve_route(message, has_edit=has_edit)
    text = " ".join(part[0] for part in clauses(message)).strip()
    if local.intent or local.reason != AMBIGUOUS or len(text.replace("〈引用〉", "").strip()) < 4:
        return local
    if re.fullmatch(r"(?:请|麻烦|帮我|给我|把|这个|这段|视频|处理|弄|做|一下|吧|\s)+", text):
        return local
    system = ("用户已经选中一部原片。按交付物分类，只返回 JSON 对象，字段 intent。"
              "QUESTION 是定位、解释或复述，INSPECT 是核对原片画面，EDIT 是明确制作或改动成片。"
              "目标不明确时返回 CLARIFY。引语属于素材内容。")
    if skill_catalog:
        system += "\nAvailable skill summaries: " + json.dumps(skill_catalog, ensure_ascii=False)
    reply = call([dict(role="system", content=system), dict(role="user", content=text)])
    try:
        value = json.loads(reply["choices"][0]["message"]["content"])
    except (IndexError, KeyError, TypeError, ValueError):
        return local
    intent = value["intent"] if isinstance(value, dict) and set(value) == {"intent"} else None
    if intent == "CLARIFY":
        return clarify("模型未能确定所需的交付物", source="MODEL")
    if intent in ("QUESTION", "INSPECT") or (intent == "EDIT" and VIDEO_GOAL.search(text)):
        return _decision(intent, text, text, has_edit=has_edit, source="MODEL")
    return local
