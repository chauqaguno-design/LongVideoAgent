"""Timed text mapping shared by the soft-subtitle and burn-in paths."""

from dataclasses import dataclass
from collections import defaultdict
from pathlib import Path
import re


@dataclass(frozen=True)
class Cue:
    start_us: int
    end_us: int
    text: str


def _time_us(s):
    hours, minutes, seconds, millis = map(int, re.split(r"[:,.]", s))
    return ((hours * 60 + minutes) * 60 + seconds) * 1_000_000 + millis * 1000


def parse_srt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.strip().replace("\r", "")):
        rows = block.splitlines()
        if len(rows) >= 3:
            start, end = rows[1].split(" --> ")
            cues.append(Cue(_time_us(start), _time_us(end), "\n".join(rows[2:])))
    return cues


def map_cues(cues, start_us, end_us, output_start_us, output_end_us):
    mapped = []
    for cue in cues:
        start = max(start_us, cue.start_us) - start_us + output_start_us
        end = min(min(end_us, cue.end_us) - start_us + output_start_us, output_end_us)
        if start < end:
            mapped.append(Cue(start, end, cue.text))
    return mapped


def soft_caption_windows(cues):
    """An MP4 timed-text track needs one visible text block at each instant."""
    changes = defaultdict(list)
    for n, cue in enumerate(cues):
        first, last = (round(t / 1000) * 1000 for t in (cue.start_us, cue.end_us))
        if first < last:
            changes[first].append((n, cue.text))
            changes[last].append((n, None))
    active, result, previous = {}, [], 0
    for time in sorted(changes):
        if active:
            text = "\n".join(active[n] for n in sorted(active))
            result.append(Cue(previous, time, text))
        for n, text in changes[time]:
            if text is None:
                active.pop(n)
            else:
                active[n] = text
        previous = time
    return result


def _srt_time(us):
    secs, ms = divmod(round(us / 1000), 1000)
    minutes, s = divmod(secs, 60)
    h, m = divmod(minutes, 60)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def write_srt(path: Path, cues):
    path.write_text("\n\n".join(
        f"{i}\n{_srt_time(c.start_us)} --> {_srt_time(c.end_us)}\n{c.text}"
        for i, c in enumerate(cues, 1)
    ) + "\n", encoding="utf-8")
