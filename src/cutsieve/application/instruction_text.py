"""Shared syntax for explicit source intervals and references in edit requests."""

from decimal import Decimal
import re


QUOTES = re.compile(
    r"""「[^」]*(?:」|$)|『[^』]*(?:』|$)|“[^”]*(?:”|$)|‘[^’]*(?:’|$)|《[^》]*(?:》|$)|"[^"]*(?:"|$)|(?<!\w)'[^']*(?:'|$)"""
)
QUOTE_END = dict(zip("「『“‘《\"'", "」』”’》\"'"))
CLOCK = r"(?:\d+:){0,2}\d+(?:\.\d{1,6})?"
INTERVAL = re.compile(
    rf"(?<![\d.:])(?:第\s*)?({CLOCK})\s*(?:秒|s)?\s*(?:至|到|[-–—~～])\s*(?:第\s*)?({CLOCK})\s*(?:秒|s)?",
    re.I,
)
NUMBER = r"(?:\d+|[零〇一二两三四五六七八九十百]+)"
ORDINAL = rf"第\s*({NUMBER})\s*(?:个|段|条|项)"
LAST = r"上一段|最后一段|末段"
REPLACEMENT = re.compile(
    rf"(?:(?:请|帮我|把|将|上(?:一)?版的?|当前的?)\s*)*(?:第\s*({NUMBER})\s*段|最后一段|末段)"
    r"\s*(?:换成|换为|替换成|替换为)\s*(.+)"
)
RESTORE = re.compile(r"(?:恢复(?:到)?|回到|还原)\s*(?:版本|v)\s*(\d+)\s*版?", re.I)
REMOVE_LAST = re.compile(rf"(?:去掉|删除|删掉|移除)\s*(?:{LAST})")
REVERSE = re.compile(r"倒序|反转顺序|(?:交换|调换|对调)(?:前?两段(?:的)?)?顺序")
SECONDS = rf"(\d+(?:\.\d{{1,6}})?|{NUMBER})\s*秒"
EXTEND = r"(?:再|多|延伸|延长|保留|留|加上|加)*\s*"
RELATIVE_TRIMS = (
    ("before_us", re.compile(r"(?:往前|向前|提前|前面|前边|前)" + EXTEND + SECONDS)),
    ("before_us", re.compile(SECONDS + r"前(?:面)?开始")),
    ("after_us", re.compile(r"(?:往后|向后|延后|后面|后边|后)" + EXTEND + SECONDS)),
    ("max_us", re.compile(r"(?:最长不超过|最多|至多|不超过|不要超过|控制在|限制在|最长|上限(?:为)?)\s*"
                          + SECONDS + r"(?:以[内下]|之内|内)?")),
    ("max_us", re.compile(SECONDS + r"(?:以内|之内)")),
)
CLAUSE = re.compile(r"[，,。；;！？!?\n]|但是|但|不过|而是|只(?=帮我|找|查|核对|看|标)")
DENIAL = re.compile(r"(?:不要|不想|不用|无需|不必|请勿|暂不|(?<!分)别|不)\s*(?:再\s*)?(?=剪|编辑|改|删|导出|预览|拼接|用|做|把|将)")


def clauses(text):
    return [DENIAL.split(part, maxsplit=1) for part in CLAUSE.split(QUOTES.sub("〈引用〉", text))]


def number(text):
    if text[0].isdecimal():
        return Decimal(text)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    total, digit = 0, 0
    for char in text.translate(str.maketrans("〇两", "零二")):
        if char in "十百":
            total += (digit or 1) * (10 if char == "十" else 100)
            digit = 0
        else:
            digit = digits[char]
    return total + digit


def quoted_literal(text):
    return text[1:-1].strip() if len(text) > 1 and text[-1] == QUOTE_END[text[0]] else ""


def timeline_operation(text):
    text = re.sub(r"^(?:(?:请|帮我)\s*)+", "", text.strip()).rstrip("。！!")
    if match := RESTORE.fullmatch(text):
        return dict(command="restore", number=int(match[1]))
    if REMOVE_LAST.fullmatch(text):
        return dict(command="remove_last")
    if REVERSE.fullmatch(text):
        return dict(command="reverse")
    return None


def replacement_request(text):
    match = REPLACEMENT.match(text.strip())
    return (int(number(match[1])) if match[1] else -1, match[2]) if match else None


def candidate_reference(message):
    """Return the candidate number, or zero when the reference is ambiguous."""
    text = " ".join(part[0] for part in clauses(message)).strip()
    if REMOVE_LAST.search(text) or replacement_request(text):
        return None
    numbers = {int(number(n)) for n in re.findall(ORDINAL, text)}
    if numbers and (re.search(r"刚才|刚刚|上次|上面|上述|候选|搜索结果|检索结果", text) or re.fullmatch(ORDINAL, text)):
        return next(iter(numbers)) if len(numbers) == 1 else 0
    return 0 if re.search(r"(?:刚才|刚刚|上面)(?:的|找到的)?(?:那段|片段|结果)|上一(?:段|个候选|条结果)", text) else None
