from __future__ import annotations

import re

from app.api.models import ExtractedFilters

_SENTENCE_SPLIT = re.compile(r"[.?!]+")
_LETTER_TOKEN = re.compile(r"[A-Za-z]+")
_REPEAT_FOUR = re.compile(r"(.)\1{3,}")
_VOWELS = set("aeiouyAEIOUY")
_STRAY_PUNCT = re.compile(r"(?:^|\s)[.?!,:;]+(?=\s|$)")
_QWERTY_RUN = "qwertyuiopasdfghjklzxcvbnm"
_QWERTY_RUN_REV = _QWERTY_RUN[::-1]

_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_NUM = r"(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)"

_STOPWORDS = {
    "a",
    "an",
    "the",
    "for",
    "to",
    "of",
    "and",
    "or",
    "with",
    "in",
    "on",
    "at",
    "my",
    "i",
    "we",
    "game",
    "games",
    "something",
    "please",
    "want",
    "looking",
}

_CATEGORY_ALIASES: list[tuple[str, str]] = [
    ("card games", "Card Game"),
    ("card game", "Card Game"),
    ("co-operative", "Cooperative"),
    ("cooperative", "Cooperative"),
    ("co-op", "Cooperative"),
    ("coop", "Cooperative"),
    ("strategy", "Strategy"),
    ("party", "Party"),
]

_LIGHT = (
    "light-hearted",
    "light hearted",
    "lighthearted",
    "beginner",
    "casual",
    "gateway",
    "filler",
    "simple",
    "easy",
    "light",
)
_MEDIUM = (
    "middleweight",
    "mid-weight",
    "midweight",
    "moderate",
    "mediocre",
    "average",
    "medium",
)
_HEAVY = (
    "brain-burner",
    "brain burner",
    "hard-core",
    "hard core",
    "hardcore",
    "difficult",
    "thinking",
    "complex",
    "crunchy",
    "thinky",
    "heavy",
    "hard",
)

_BEST_RE = re.compile(rf"\bbest\s+(?:with|at)\s+({_NUM})\b", re.I)
_REC_RE = re.compile(rf"\brecommended\s+(?:for|with)\s+({_NUM})\b", re.I)
_PLAYER_RE = re.compile(
    rf"\b(?:for\s+)?({_NUM})\s+players?\b|\b({_NUM})-player\b|\bfor\s+({_NUM})\b",
    re.I,
)
_HOUR_RE = re.compile(r"\b(?:under|less\s+than)\s+an\s+hour\b", re.I)
_TIME_RE = re.compile(
    rf"(?:under|less\s+than|max|at\s+most|≤|<=)\s*({_NUM})\s*(?:minutes|minute|mins|min)\b",
    re.I,
)
_WEIGHT_MIN_RE = re.compile(
    r"\bweight\s*(?:>|>=|≥)\s*(\d+(?:\.\d+)?)|"
    r"\bheavier\s+than\s+(\d+(?:\.\d+)?)|"
    r"\bweight\s+at\s+least\s+(\d+(?:\.\d+)?)\b",
    re.I,
)
_WEIGHT_MAX_RE = re.compile(
    r"\bweight\s*(?:<|<=|≤)\s*(\d+(?:\.\d+)?)|"
    r"\bweight\s+(?:under|below|at\s+most)\s+(\d+(?:\.\d+)?)\b",
    re.I,
)
_MAX_AGE_RE = re.compile(
    rf"\b(?:for\s+(?:my\s+)?)?({_NUM})[-\s]year[-\s]olds?\b|\bfor\s+kids\s+({_NUM})\b",
    re.I,
)
_MIN_AGE_RE = re.compile(rf"\b(?:ages?\s+)?(\d+)\+", re.I)
_ADULTS_RE = re.compile(r"\badults\b", re.I)
_MIN_YEAR_RE = re.compile(r"\b(?:after|since|from)\s+((?:19|20)\d{2})\b", re.I)
_MAX_YEAR_RE = re.compile(r"\bbefore\s+((?:19|20)\d{2})\b", re.I)
_SIMILAR_TRIGGER_RE = re.compile(
    r"(?:similar\s+to|alternatives?\s+to|games?\s+like|something\s+like|\blike)\s+",
    re.I,
)


def sentence_count(query: str) -> int:
    return sum(1 for part in _SENTENCE_SPLIT.split(query) if part.strip())


def _is_gibberish_token(token: str) -> bool:
    if len(token) < 3:
        return False
    if _REPEAT_FOUR.search(token):
        return True
    if not any(char in _VOWELS for char in token):
        return True
    lower = token.lower()
    if len(lower) >= 4 and (lower in _QWERTY_RUN or lower in _QWERTY_RUN_REV):
        return True
    return False


def sanitize_gibberish(query: str) -> str:
    def _replace(match: re.Match[str]) -> str:
        token = match.group(0)
        return " " if _is_gibberish_token(token) else token

    cleaned = _LETTER_TOKEN.sub(_replace, query)
    cleaned = _STRAY_PUNCT.sub(" ", cleaned)
    return re.sub(r"\s+", " ", cleaned).strip()


def extract_filters_from_text(query: str) -> ExtractedFilters:
    text = query.strip()
    if not text:
        return ExtractedFilters()

    used: list[tuple[int, int]] = []
    data: dict = {}

    def _take(match: re.Match | None, *group_indexes: int) -> str | None:
        if match is None:
            return None
        used.append(match.span())
        for i in group_indexes:
            raw = match.group(i)
            if raw is not None:
                return raw
        return match.group(0)

    best = _BEST_RE.search(text)
    rec = _REC_RE.search(text)
    if best:
        data["best_with_player_count"] = _parse_num(_take(best, 1))
    if rec:
        data["recommended_with_player_count"] = _parse_num(_take(rec, 1))
    if "best_with_player_count" not in data and "recommended_with_player_count" not in data:
        for player in _PLAYER_RE.finditer(text):
            raw = next(g for g in player.groups() if g is not None)
            n = _parse_num(raw)
            if n <= 10:
                used.append(player.span())
                data["player_count"] = n
                break

    hour = _HOUR_RE.search(text)
    if hour:
        _take(hour, 0)
        data["max_play_time_minutes"] = 60
    else:
        time_m = _TIME_RE.search(text)
        if time_m:
            data["max_play_time_minutes"] = _parse_num(_take(time_m, 1))

    complexity = _first_complexity(text)
    if complexity is not None:
        bucket, span = complexity
        used.append(span)
        data["complexity"] = bucket

    wmin = _WEIGHT_MIN_RE.search(text)
    if wmin:
        data["min_weight"] = float(_take(wmin, 1, 2, 3))
    wmax = _WEIGHT_MAX_RE.search(text)
    if wmax:
        data["max_weight"] = float(_take(wmax, 1, 2))

    max_age_m = _MAX_AGE_RE.search(text)
    if max_age_m:
        data["max_age"] = _parse_num(_take(max_age_m, 1, 2))
    elif _ADULTS_RE.search(text):
        _take(_ADULTS_RE.search(text), 0)
        data["min_age"] = 18
    else:
        min_age_m = _MIN_AGE_RE.search(text)
        if min_age_m:
            data["min_age"] = int(_take(min_age_m, 1))

    ymin = _MIN_YEAR_RE.search(text)
    if ymin:
        data["min_year"] = int(_take(ymin, 1))
    ymax = _MAX_YEAR_RE.search(text)
    if ymax:
        data["max_year"] = int(_take(ymax, 1))

    similar = _extract_similar_to(text, used)
    if similar is not None:
        name, span = similar
        used.append(span)
        data["similar_to"] = name

    categories, cat_spans = _extract_categories(text)
    used.extend(cat_spans)
    if categories:
        data["categories"] = categories

    keywords = _keywords(text, used)
    if keywords:
        data["keywords"] = keywords

    return ExtractedFilters.model_validate(data)


def _parse_num(raw: str | None) -> int:
    if raw is None:
        raise ValueError("missing number")
    key = raw.lower()
    if key in _WORD_NUMBERS:
        return _WORD_NUMBERS[key]
    return int(raw)


def _phrase_regex(phrases: tuple[str, ...]) -> re.Pattern:
    parts = [re.escape(p) for p in sorted(phrases, key=len, reverse=True)]
    return re.compile(r"\b(?:" + "|".join(parts) + r")\b", re.I)


_LIGHT_RE = _phrase_regex(_LIGHT)
_MEDIUM_RE = _phrase_regex(_MEDIUM)
_HEAVY_RE = _phrase_regex(_HEAVY)


def _first_complexity(text: str) -> tuple[str, tuple[int, int]] | None:
    hits: list[tuple[int, str, tuple[int, int]]] = []
    for bucket, cre in (
        ("light", _LIGHT_RE),
        ("medium", _MEDIUM_RE),
        ("heavy", _HEAVY_RE),
    ):
        match = cre.search(text)
        if match:
            hits.append((match.start(), bucket, match.span()))
    if not hits:
        return None
    hits.sort(key=lambda h: h[0])
    _, bucket, span = hits[0]
    return bucket, span


def _extract_categories(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    found: list[str] = []
    spans: list[tuple[int, int]] = []
    seen: set[str] = set()
    lower = text.lower()
    for alias, canonical in _CATEGORY_ALIASES:
        start = 0
        while True:
            idx = lower.find(alias, start)
            if idx < 0:
                break
            if idx > 0 and lower[idx - 1].isalnum():
                start = idx + 1
                continue
            end = idx + len(alias)
            if end < len(lower) and lower[end].isalnum():
                start = idx + 1
                continue
            if canonical not in seen:
                seen.add(canonical)
                found.append(canonical)
            spans.append((idx, end))
            start = end
    return found, spans


def _category_alias_at_start(name: str) -> bool:
    lower = name.lower().strip()
    for alias, _canonical in _CATEGORY_ALIASES:
        if lower == alias or lower.startswith(alias + " "):
            return True
    return False


def _extract_similar_to(
    text: str, used: list[tuple[int, int]]
) -> tuple[str, tuple[int, int]] | None:
    match = _SIMILAR_TRIGGER_RE.search(text)
    if match is None:
        return None
    name_start = match.end()
    stops = [len(text)]
    for start, _end in used:
        if start >= name_start:
            stops.append(start)
    for extra in _SIMILAR_TRIGGER_RE.finditer(text):
        if extra.start() >= name_start:
            stops.append(extra.start())
    for punct in re.finditer(r"[.?!]", text):
        if punct.start() >= name_start:
            stops.append(punct.start())
    name_end = min(stops)
    raw = text[name_start:name_end].strip()
    raw = raw.strip("\"'")
    raw = re.sub(r"[,:;]+$", "", raw).strip()
    if not raw:
        return None
    trigger = match.group(0).lower()
    if re.fullmatch(r"like\s+", trigger) and _category_alias_at_start(raw):
        return None
    return raw, (match.start(), name_end)


def _overlaps(span: tuple[int, int], used: list[tuple[int, int]]) -> bool:
    a, b = span
    for c, d in used:
        if a < d and c < b:
            return True
    return False


def _keywords(text: str, used: list[tuple[int, int]]) -> list[str]:
    words: list[str] = []
    for match in re.finditer(r"[A-Za-z][A-Za-z'-]*", text):
        if _overlaps(match.span(), used):
            continue
        word = match.group(0).lower()
        if word in _STOPWORDS or word in _WORD_NUMBERS:
            continue
        if word not in words:
            words.append(word)
    return words
