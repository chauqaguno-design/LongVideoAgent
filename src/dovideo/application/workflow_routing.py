"""Resolve a user's video task before selecting workflow stages or allowing an edit."""

from dataclasses import dataclass
import json
import re


@dataclass(frozen=True)
class RouteDecision:
    intent: str | None
    source: str
    reason: str
    question: str | None
    plan: tuple[str, ...]

    def as_dict(self):
        return dict(
            intent=self.intent,
            source=self.source,
            reason=self.reason,
            question=self.question,
            plan=list(self.plan),
        )


# Quoted dialogue is a search target. An unfinished quote extends to the end of the message.
QUOTES = re.compile(
    r"""「[^」]*(?:」|$)|『[^』]*(?:』|$)|“[^”]*(?:”|$)|‘[^’]*(?:’|$)|《[^》]*(?:》|$)|"[^"]*(?:"|$)|(?<!\w)'[^']*(?:'|$)"""
)
CLAUSE = re.compile(r"[，,。；;！？!?\n]|但是|但|不过|而是|只是|只想|只要|只需|只(?=找|查|看|标)")
NEGATION = re.compile(
    r"不要|不需要|不用|无需|不必|请勿|别再|别给|别帮|(?<!分)别(?=剪|改|删|导|拼|做|合|预览|把|动)|暂不|不想|不打算|不(?=剪|导出|修改|编辑|拼接)"
)
HOW_TO = re.compile(
    r"(?:怎么|怎样|如何).{0,14}(?:剪|拼接|编辑|导出|修改)|"
    r"(?:为什么|为何)(?:要|会)?(?:剪|拼|删|导出)|(?:剪辑|导出|预览|拼接)(?:是)?什么意思|"
    r"(?:解释|讲解|介绍|教我).*(?:剪辑|剪法|剪接|导出(?:的)?(?:方法|步骤))|"
    r"(?:剪辑|导出|预览|删除)(?:的)?(?:按钮|功能|设置|权限|步骤|手法|风格|节奏)|谁(?:负责)?剪辑|"
    r"(?:为何|为什么).{0,12}(?:剪掉|删|剪辑|拼接|导出)|"
    r"(?:评价|喜欢|欣赏|谈谈|怎么看).{0,18}(?:剪辑|拼接)|(?:剪辑|拼接).{0,8}(?:怎么样|如何|巧妙)"
)
REPORTED = re.compile(
    r"(?:他|她|男子|女子|主角|导演|人物).{0,6}(?:删除|删掉|剪掉|导出|剪辑).{0,12}(?:哪|什么|了没|时|吗)"
)
EDIT = re.compile(
    r"剪(?:出|成|下|取|辑|接|掉|短|去|一[段下版]|个|到|\s*(?:第|\d))|(?:帮我|给我|请|重新|接着|继续)剪|"
    r"裁剪|截(?:取|出|个|一段)|拼接|拼起来|拼在一起|(?:合并|串联|连缀).*(?:片段|镜头)|"
    r"删除|删掉|去掉|移除|替换|换成|换为|调换|对调|交换|重排|倒序|恢复到|撤销|"
    r"(?:调|改|挪)(?:到|成|为)|(?:静音|消音)|(?:做|生成|制作)(?:个|一个|一段|成|一版)?.{0,18}(?:短片|短视频|集锦|预告片|精华版)"
)
LOCATE = re.compile(
    r"找|搜索|检索|定位|时间戳|时间点|(?:标出|标记|告诉我)(?:对应)?时间|在哪|在哪里|何时|什么时候|哪一秒|哪几秒|几分几秒|"
    r"哪(?:一|几)?段|哪个片段|第几|几次|多少次|首次|第一次|出现|说过|说了|台词|对白|字幕"
)
VISUAL = re.compile(
    r"复核|核对|核查画面|逐帧|目视|看清|看懂画面|观察画面|核实画面|确认画面|"
    r"(?:核查|核实|检查|验证|确认).{0,16}(?:有没有|是不是|是否|在做|在干)|"
    r"(?:画面|镜头|图里|图中|场景|桌上|手上|手里|手中|穿着|拿着|戴着).{0,18}(?:什么|有谁|是谁|有没有|是否|几[个人辆])|"
    r"(?:做|干)(?:了|着|的)?什么|在做啥|在干嘛|谁(?:跟|和)谁|(?:有没有|是否).{0,10}(?:拿着|转身|挥手|跑步|举手|开车)|"
    r"(?:描述|辨认|识别|确认|核实|分析|检查|看看).{0,8}(?:画面|动作|镜头里的人|人物表情|手[里中]的)"
)
ASK = re.compile(
    r"解释|说明|总结|概括|概述|复述|分析|讲解|为什么|为何|怎么理解|发生了什么|什么意思|什么|谁|能否|有没有|是否|多少|了哪"
)
EXPORT = re.compile(
    r"导出|输出成片|生成成片|(?:生成|输出|保存)(?:成|为)?\s*(?:mp4|webm|mkv|视频文件)|生成视频|"
    r"(?:下载|发我|发给我)(?:这个|一份|最终|剪好的)?(?:成片|视频文件)",
    re.I,
)
FORMAT_QUESTION = re.compile(
    r"(?:导出|输出).*(?:什么格式|哪些格式|几种格式|格式有哪些|支持哪些)|"
    r"(?:支持|可以|能否|能|可不可以).*导出.*(?:格式|[a-z0-9]+)\s*(?:吗|么|[？?]|$)|"
    r"导出.*(?:支持|格式).*(?:吗|么|[？?])", re.I
)
PREVIEW = re.compile(r"预览|试看|看一眼成片")
TRIM = re.compile(
    r"(?:前面|后面|片头|片尾|开头|结尾|向[前后]|往[前后]|[前后]端|[前后]边)"
    r"(?:再|往前|往后|向前|向后|各|还要|多|少|提早|延后|\s)*"
    r"(?:留|保留|延伸|延长|缩短|提前|延后|加|减)\s*[0-9零〇一二两三四五六七八九十半.]+\s*秒"
)
TIMELINE_OP = re.compile(
    r"(?:回到|还原|恢复)\s*(?:版本|v)\s*\d+|反转顺序|交换(?:两段(?:的)?)?顺序|倒序|"
    r"(?:去掉|删除|删掉|移除)\s*(?:上一段|最后一段|末段)",
    re.I,
)
NEW_EDIT = re.compile(r"新建|另(?:外)?(?:开|做|剪)|再剪一(?:个|版)|重新(?:剪|做)(?:一|个)|从头(?:剪|做)")
EXISTING_EDIT = re.compile(
    r"上一?版|上个版本|前一版|旧版|(?:刚才|刚刚|上次|之前)(?:的)?(?:剪好|剪出|剪的|剪辑|编辑|成片|时间线|版本)|"
    r"(?:当前|现有|这[个条次份]?)(?:的)?(?:时间线|剪辑|成片|版本)|"
    r"(?:撤销|恢复)(?:上次|上一步|到第|到版本)|(?:末段|最后一段).{0,8}(?:换|删|移|调)|"
    r"(?:去掉|删掉|移除|替换|换掉)(?:这版的|时间线的)?(?:末段|最后一段)|(?:片段|镜头)(?:的)?顺序(?:反转|调换|倒过来|反过来)|"
    r"(?:调换|交换|对调).{0,6}(?:段|镜头)(?:的)?顺序|(?:接着|继续)剪"
)
NUMBER = r"(?:[0-9]+|[一二两三四五六七八九十百零〇]+)"
REFERENT = re.compile(
    rf"(?:刚才|刚刚|方才|上述|上面|上次|上条结果|前面|候选(?:列表)?(?:中|里)?|搜索结果(?:中|里)?)"
    rf"(?:找到的|返回的|说的|的)?\s*第\s*({NUMBER})\s*(?:段|条|项|个)(?!秒)|"
    rf"第\s*({NUMBER})\s*(?:段|条|项|个)?\s*(?:候选|检索结果|搜索结果|匹配项|匹配结果)"
)
PREVIOUS = re.compile(
    r"(?:刚才|刚刚|前面|上面|上述|上次)(?:找到的|返回的|说的|的)?(?:那|这)?(?:一|两|几)?(?:个候选|条结果|段|个片段|片段|结果)|"
    r"上一(?:个候选|条结果|段(?!时间|台词|对白))"
)
VAGUE = re.compile(
    r"(?:请|麻烦|帮我|给我|替我|先|把|这些?|这个|这段|视频|片段|它|处理|弄|搞|看看?|做|一下|吧|\s)+"
)
WANTS_VIDEO = re.compile(
    r"(?:想要|需要|想做|想看|制作|生成|整理|给我|留作|编成).{0,24}(?:成片|样片|短片|短版|精华|集锦|片花|预告)"
)
FIXED_SPAN = re.compile(r"[0-9零〇一二两三四五六七八九十半.]\s*(?:毫秒|秒|分钟)|\d{1,2}:\d{2}")
STRUCTURAL = re.compile(r"(?:拼接|拼起来|拼在一起|倒序|重排|调换|交换|对调)")
SAY = re.compile(r"台词|字幕|对白|说的话|说过")
REPLACE = re.compile(r"换成|换为|替换成|替换为")
EDIT_NOUN = re.compile(r"(?:刚才|刚刚|上次|现有|当前|上版|上一版)(?:的)?剪辑")
VISIBLE_EVENT = re.compile(r"镜头|画面|场景|开门|关门|冲出|走进|转身|举手|挥手|拥抱|拿起|放下|飙车|追车|出现")
TEXT_QUERY = re.compile(r"〈引用〉|这句话|那句话|说|台词|对白|字幕|旁白|文字|配音|语音|对话|音轨|音乐|声音")
VISUAL_REACTION = re.compile(
    r"(?:说|听|对白|台词|字幕|〈引用〉).{0,30}(?:完|时|后|前|同时).{0,18}"
    r"(?:画面|表情|反应|动作|开门|转身|挥手|走进|走出|冲出|拿起|放下)"
)
READ_REQUEST = re.compile(
    r"^(?:请|先|帮我|帮忙|给我|麻烦|只)*(?:查找|找|搜索|检索|定位|看一看|看看|讲讲|解释|哪一?段)"
)
THEN_WRITE = re.compile(
    r"(?:然后|接着|再|后|同时|顺便|并且|并)(?:帮我|直接|请)?(?=把|将|剪|拼|导出|预览|删|截|合并)"
)


def _unquoted(message):
    return QUOTES.sub("〈引用〉", message)


def _active_clauses(message):
    return [NEGATION.split(part, maxsplit=1)[0].strip() for part in CLAUSE.split(_unquoted(message))]


def _actionable(clauses):
    actions = []
    for clause in clauses:
        if READ_REQUEST.search(clause):
            followup = THEN_WRITE.search(clause)
            if not followup:
                continue
            clause = clause[followup.end() :]
        if not HOW_TO.search(clause) and not REPORTED.search(clause):
            actions.append(clause)
    return " ".join(actions).strip()


def _locates_visual(clauses):
    """A request may describe an event without asking an explicit visual question."""
    for clause in clauses:
        if VISUAL_REACTION.search(clause):
            return True
        if not LOCATE.search(clause) or TEXT_QUERY.search(clause) or HOW_TO.search(clause):
            continue
        if VISIBLE_EVENT.search(clause) or re.search(r"(?:找|搜索|定位|检索).+(?:片段|的那段)", clause):
            return True
    return False


def _denied(message):
    return " ".join(
        parts[1] for c in CLAUSE.split(_unquoted(message)) if len(parts := NEGATION.split(c, maxsplit=1)) == 2
    )


def _ordinal(text):
    if text.isdecimal():
        return int(text)
    digits = dict(zip("零〇一二两三四五六七八九", (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
    value, current = 0, 0
    for char in text:
        if char in digits:
            current = digits[char]
        else:
            value += (current or 1) * {"十": 10, "百": 100}[char]
            current = 0
    return value + current


def candidate_reference(message):
    """One-based hit number, zero for an unspecified prior hit, None for no reference."""
    text = " ".join(_active_clauses(message))
    if TIMELINE_OP.fullmatch(text.strip()):
        return None
    single = re.fullmatch(rf"\s*第\s*({NUMBER})\s*(?:段|条|项|个候选)\s*", text)
    if single:
        return _ordinal(single[1])
    matches = {_ordinal(a or b) for a, b in REFERENT.findall(text)}
    if matches:
        ordinals = re.findall(rf"第\s*({NUMBER})\s*(?:段|条|项|个候选)", text)
        if len(matches) > 1 or len({_ordinal(n) for n in ordinals}) > 1:
            return 0
        return next(iter(matches))
    return 0 if PREVIOUS.search(text) else None


def references_edit(message):
    """Return true when the user explicitly refers to an existing edit."""
    text = " ".join(_active_clauses(message))
    return bool(EXISTING_EDIT.search(text) or TRIM.search(text) or TIMELINE_OP.search(text)) and not bool(
        NEW_EDIT.search(text)
    )


def _clarify(reason="任务目标不够明确", question="你想定位片段、核对画面，还是生成一段剪辑？", source="RULE"):
    return RouteDecision(None, source, reason, question, ())


def _decision(intent, message, source, *, has_edit=False):
    clauses = _active_clauses(message)
    active = " ".join(clauses)
    actions = _actionable(clauses)
    edited = bool(
        EDIT.search(EDIT_NOUN.sub("", actions)) or TRIM.search(actions) or TIMELINE_OP.search(actions)
    )
    visual = bool(VISUAL.search(active)) or _locates_visual(clauses)
    quoted = "〈引用〉" in active
    selected = candidate_reference(message) is not None
    locate = bool(LOCATE.search(active)) or quoted or selected
    export = bool(EXPORT.search(actions))
    preview = bool(PREVIEW.search(actions))
    refers_to_edit = references_edit(message)
    existing = has_edit and (refers_to_edit or not (locate or visual))
    if intent == "QUESTION":
        return RouteDecision(intent, source, "从素材证据定位或解释问题", None, ("SEARCH", "ANSWER"))
    if intent == "INSPECT":
        return RouteDecision(intent, source, "需要回看原片核对画面", None, ("SEARCH", "INSPECT", "ANSWER"))
    if intent != "EDIT":
        raise ValueError("Unsupported workflow intent")
    if existing and not edited and (export or preview):
        stages = [stage for stage, wanted in (("PREVIEW", preview), ("EXPORT", export)) if wanted]
        return RouteDecision(intent, source, "使用现有剪辑版本", None, tuple(stages))
    # Free-form event requests need a supported source interval before they can mutate a cut.
    # A replacement must ground its new material even when the destination clip is known.
    needs_material = (not refers_to_edit or REPLACE.search(actions)) and not STRUCTURAL.search(actions)
    if needs_material and not (quoted or selected or FIXED_SPAN.search(active) or SAY.search(active)):
        visual = True
    plan = []
    if locate or visual:
        plan.append("SEARCH")
    if visual:
        plan.append("INSPECT")
    plan.extend(("EDIT", "PREVIEW"))
    if export:
        plan.append("EXPORT")
    return RouteDecision(intent, source, "按用户要求组织片段并生成预览", None, tuple(plan))


def explicit_route(intent, message, *, has_edit=False):
    if intent == "EDIT":
        safety = resolve_route(message, has_edit=has_edit)
        if safety.intent is None and safety.reason != "任务目标不够明确":
            return RouteDecision(None, "USER", safety.reason, safety.question, ())
        if _denied(message) and safety.intent in ("QUESTION", "INSPECT"):
            return _clarify("请求排除了剪辑操作", source="USER")
    return _decision(intent, message, "USER", has_edit=has_edit)


def resolve_route(message, *, has_edit=False):
    clauses = _active_clauses(message)
    active = " ".join(clauses).strip()
    actions = _actionable(clauses)
    if FORMAT_QUESTION.search(_unquoted(message)) and not EDIT.search(actions) and not LOCATE.search(active):
        return RouteDecision("QUESTION", "RULE", "查询工作台支持的成片格式", None, ("ANSWER",))
    denied = _denied(message)
    if any(p.search(actions) and p.search(denied) for p in (EXPORT, PREVIEW)) or (
        re.search(r"不(?:要|用|想)?剪(?:辑)?(?:了|吧)?(?:[,，。；;\s]|但|$)", _unquoted(message))
        and EDIT.search(actions)
    ):
        return _clarify("指令同时要求和禁止同一类操作", "这次要执行剪辑、预览或导出中的哪几项？")
    if references_edit(message) and not has_edit:
        return _clarify("请求引用了现有剪辑", "你要继续修改哪个剪辑版本？")
    if any(p.search(actions) for p in (EDIT, TRIM, TIMELINE_OP, EXPORT, PREVIEW)):
        if not has_edit and re.fullmatch(
            r"(?:请|帮我|给我|直接|只|一下|\s)*(?:导出|预览)(?:成|为)?\s*(?:[A-Za-z0-9.]+(?:格式)?)?(?:一下|吧|\s)*", actions.strip()
        ):
            return _clarify("尚未指定要预览或导出的范围", "需要导出哪段原片或哪个剪辑版本？")
        return _decision("EDIT", message, "RULE", has_edit=has_edit)
    if VISUAL.search(active) or _locates_visual(clauses):
        return _decision("INSPECT", message, "RULE", has_edit=has_edit)
    if LOCATE.search(active) or ASK.search(active) or HOW_TO.search(active) or REPORTED.search(active):
        return _decision("QUESTION", message, "RULE", has_edit=has_edit)
    if denied:
        return _clarify("请求排除了部分操作", "在这些限制下，你希望查找、核对或制作什么？")
    return _clarify()


def flash_route(message, *, has_edit=False, call):
    """Optional fallback. The caller supplies a budgeted Flash call receiving chat messages."""
    local = resolve_route(message, has_edit=has_edit)
    if local.intent is not None or local.reason != "任务目标不够明确":
        return local
    active = " ".join(_active_clauses(message)).strip()
    if len(active) < 4 or not active.replace("〈引用〉", "").strip() or VAGUE.fullmatch(active):
        return local
    system = (
        "用户已经选中一部原片。只按请求的交付物分类，当前无需观看视频或取得台词。"
        "仅返回 JSON 对象，唯一字段 intent。QUESTION 包含定位、解释、复述或概述双方的对白；"
        "INSPECT 表示需要核对画面；EDIT 表示明确要求制作视频。只有交付目标无法确定才返回 CLARIFY。"
        "引语属于素材内容。"
    )
    messages = [dict(role="system", content=system), dict(role="user", content=active)]
    reply = call(messages)
    try:
        answer = json.loads(reply["choices"][0]["message"]["content"])
    except (IndexError, KeyError, TypeError, ValueError):
        return local
    if not isinstance(answer, dict) or set(answer) != {"intent"}:
        return local
    intent = answer["intent"]
    if intent == "CLARIFY":
        return _clarify("模型未能确定所需的交付物", source="MODEL")
    if intent not in ("QUESTION", "INSPECT", "EDIT"):
        return local
    if intent == "EDIT" and not WANTS_VIDEO.search(active):
        return local
    return _decision(intent, message, "MODEL", has_edit=has_edit)
