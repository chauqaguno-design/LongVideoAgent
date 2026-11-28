"""Recognize literal quotations in evidence search queries."""

import re


QUOTES = re.compile(
    r"""「[^」]*(?:」|$)|『[^』]*(?:』|$)|“[^”]*(?:”|$)|‘[^’]*(?:’|$)|《[^》]*(?:》|$)|"[^"]*(?:"|$)|(?<!\w)'[^']*(?:'|$)"""
)
QUOTE_END = dict(zip("「『“‘《\"'", "」』”’》\"'"))


def quoted_literal(text):
    return text[1:-1].strip() if len(text) > 1 and text[-1] == QUOTE_END[text[0]] else ""
