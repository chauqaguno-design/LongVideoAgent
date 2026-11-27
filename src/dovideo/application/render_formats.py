"""Containers supported by the timeline exporter. Previews always use MP4."""

import re

from .instruction_text import QUOTES, clauses
from .workflow_state import NeedsInput


EXPORT_MIME = {"mp4": "video/mp4", "webm": "video/webm", "mkv": "video/x-matroska"}
EXPORT_FORMATS = tuple(EXPORT_MIME)
EXPORT = re.compile(r"导出|输出(?:成片|视频文件)|(?:输出|保存|生成)(?:为|成)?\s*\.?[a-z][a-z0-9]*|(?:下载|发给我)成片", re.I)
PREVIEW = re.compile(r"预览|试看")
FORMAT_QUESTION = re.compile(
    r"(?:导出|输出).*(?:什么格式|哪些格式|几种格式|格式有哪些|支持哪些)|"
    r"(?:支持|可以|能|只能|能否).*导出.*[a-z0-9]\s*(?:吗|么|[？?]|$)|导出.*格式.*[？?]",
    re.I,
)
_FORMATS = r"[a-z][a-z0-9]*(?:\s*[/、或和]\s*[a-z][a-z0-9]*)*"
_EXPORT_FORMAT = re.compile(
    QUOTES.pattern
    + rf"|(?:导出|输出|保存|生成)(?:为|成)?\s*\.?(?P<after>{_FORMATS})(?:\s*格式)?"
      rf"|(?:以|用)?\.?(?P<before>{_FORMATS})\s*(?:格式)?导出",
    re.I,
)


def requests_export(text):
    return any(EXPORT.search(part[0]) for part in clauses(text))


def export_instruction(text, reply=None):
    """Extract a requested container without touching quoted dialogue."""
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
    if len(requested) == 1 and requested <= EXPORT_MIME.keys():
        return instruction, requested.pop()
    options = [name.upper() for name in EXPORT_FORMATS]
    raise NeedsInput("每次导出一个文件。请选择本次使用的格式：" + "、".join(options) + "。", options)
