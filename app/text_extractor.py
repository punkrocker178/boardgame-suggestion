from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"[.?!]+")


def sentence_count(query: str) -> int:
    return sum(1 for part in _SENTENCE_SPLIT.split(query) if part.strip())
