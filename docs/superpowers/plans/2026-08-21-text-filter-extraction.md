# Text-first query extraction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse `/recommend` queries into `ExtractedFilters` with regex/word lists first, and call the existing LLM extractor only as fallback.

**Architecture:** `extract_filters_from_text` always runs. `resolve_filters` calls `extract_filters` (LLM) only when sentence count is `> 3`, or when text set neither `similar_to` nor any hard filter. LLM success replaces the text result; LLM skip/failure/`None` keeps text. Synthesis still requires the LLM.

**Tech Stack:** Python 3, stdlib `re`, existing Pydantic `ExtractedFilters`, pytest, FastAPI `TestClient`

**Spec:** `docs/superpowers/specs/2026-08-20-text-filter-extraction-design.md`

## Global Constraints

- No spaCy or other NLP libraries
- LLM replaces text filters; do not merge fields
- `similar_to` is not added to `HARD_FILTER_FIELDS`
- Skip LLM on short queries when text set `similar_to` **or** hard filters
- LLM fallback on `sentence_count > 3` even if text found filters
- On LLM error or `llm is None`, keep text filters; no 502 at extraction
- Synthesis 502 `LLM unavailable` is unchanged
- Category labels: small alias map only; `apply_category_normalization` still runs in retrieval
- Do not implement min play time or player ranges (`3-5 players`)

---

## File map

| File | Responsibility |
|------|----------------|
| `app/text_extractor.py` | `sentence_count`, `extract_filters_from_text` |
| `tests/test_text_extractor.py` | Table-driven field tests + sentence count |
| `app/query_extractor.py` | Keep LLM `extract_filters`; add `should_use_llm`, `resolve_filters` |
| `tests/test_resolve_filters.py` | Fallback gating and LLM failure |
| `app/main.py` | Call `resolve_filters`; 502 only if LLM missing/fails at **synthesis** |
| `tests/test_api.py` | Patch `resolve_filters`; text-extract path with `llm is None` |
| `app/sql_filters.py` | Unchanged |
| `app/llm_parsing.py` | Unchanged |

---

### Task 1: `sentence_count`

**Files:**
- Create: `app/text_extractor.py`
- Create: `tests/test_text_extractor.py`

**Interfaces:**
- Consumes: raw query string
- Produces: `sentence_count(query: str) -> int`

- [ ] **Step 1: Write the failing test**

Create `tests/test_text_extractor.py`:

```python
import pytest

from app.text_extractor import sentence_count


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("one", 1),
        ("a. b. c", 3),
        ("a. b. c. d", 4),
        ("Really? Yes!", 2),
        ("Hi.  ", 1),
        ("", 0),
    ],
)
def test_sentence_count(query: str, expected: int) -> None:
    assert sentence_count(query) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_text_extractor.py::test_sentence_count -v`

Expected: FAIL with `ImportError` or `sentence_count` not defined

- [ ] **Step 3: Write minimal implementation**

Create `app/text_extractor.py`:

```python
from __future__ import annotations

import re

_SENTENCE_SPLIT = re.compile(r"[.?!]+")


def sentence_count(query: str) -> int:
    return sum(1 for part in _SENTENCE_SPLIT.split(query) if part.strip())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_text_extractor.py::test_sentence_count -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/text_extractor.py tests/test_text_extractor.py
git commit -m "$(cat <<'EOF'
feat: count query sentences for LLM extraction fallback

EOF
)"
```

---

### Task 2: `extract_filters_from_text`

**Files:**
- Modify: `app/text_extractor.py`
- Modify: `tests/test_text_extractor.py`

**Interfaces:**
- Consumes: query string
- Produces: `extract_filters_from_text(query: str) -> ExtractedFilters`
- Must set only explicitly matched fields. Empty/non-matching → all-null model (blank `similar_to` already normalizes to null via `ExtractedFilters`).

Implement **all** spec field rules in this task (players, play time, complexity/weight, age, year, similar_to, categories, keywords). Later tasks only orchestrate LLM fallback.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_text_extractor.py`:

```python
from app.models import ExtractedFilters
from app.text_extractor import extract_filters_from_text, sentence_count


def test_empty_query_returns_empty_filters() -> None:
    assert extract_filters_from_text("") == ExtractedFilters()
    assert extract_filters_from_text("   ") == ExtractedFilters()


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("for 4 players", ExtractedFilters(player_count=4)),
        ("4-player game", ExtractedFilters(player_count=4)),
        ("four players", ExtractedFilters(player_count=4)),
        ("best with 4", ExtractedFilters(best_with_player_count=4)),
        ("best at 4", ExtractedFilters(best_with_player_count=4)),
        ("recommended for 3", ExtractedFilters(recommended_with_player_count=3)),
        ("recommended with 3", ExtractedFilters(recommended_with_player_count=3)),
    ],
)
def test_player_phrases(query: str, expected: ExtractedFilters) -> None:
    got = extract_filters_from_text(query)
    assert got.player_count == expected.player_count
    assert got.best_with_player_count == expected.best_with_player_count
    assert got.recommended_with_player_count == expected.recommended_with_player_count


def test_plain_player_count_does_not_set_best() -> None:
    got = extract_filters_from_text("for 4 players")
    assert got.player_count == 4
    assert got.best_with_player_count is None


@pytest.mark.parametrize(
    ("query", "minutes"),
    [
        ("under 45 minutes", 45),
        ("max 60 min", 60),
        ("≤ 90 minutes", 90),
        ("under an hour", 60),
    ],
)
def test_play_time(query: str, minutes: int) -> None:
    assert extract_filters_from_text(query).max_play_time_minutes == minutes


@pytest.mark.parametrize(
    ("query", "bucket"),
    [
        ("light game", "light"),
        ("casual game", "light"),
        ("light-hearted game", "light"),
        ("gateway game", "light"),
        ("moderate weight", "medium"),
        ("mediocre complexity", "medium"),
        ("average complexity", "medium"),
        ("midweight game", "medium"),
        ("heavy game", "heavy"),
        ("hardcore game", "heavy"),
        ("hard core game", "heavy"),
        ("thinking game", "heavy"),
        ("brain-burner", "heavy"),
        ("crunchy euro", "heavy"),
    ],
)
def test_complexity_synonyms(query: str, bucket: str) -> None:
    got = extract_filters_from_text(query)
    assert got.complexity == bucket
    assert got.min_weight is None
    assert got.max_weight is None


def test_numeric_weight_without_bucket() -> None:
    got = extract_filters_from_text("weight > 3")
    assert got.min_weight == 3
    assert got.complexity is None


def test_weight_under_without_bucket() -> None:
    got = extract_filters_from_text("weight under 2")
    assert got.max_weight == 2
    assert got.complexity is None


def test_bucket_and_numeric_weight_both_set() -> None:
    got = extract_filters_from_text("heavy game with weight > 3.5")
    assert got.complexity == "heavy"
    assert got.min_weight == 3.5


def test_party_is_not_complexity() -> None:
    got = extract_filters_from_text("party game")
    assert got.complexity is None
    assert got.categories == ["Party"]


@pytest.mark.parametrize(
    ("query", "min_age", "max_age"),
    [
        ("14+", 14, None),
        ("ages 14+", 14, None),
        ("adults", 18, None),
        ("8+", 8, None),
        ("for my 8-year-old", None, 8),
        ("for kids 8", None, 8),
        ("8 year old", None, 8),
    ],
)
def test_age(query: str, min_age: int | None, max_age: int | None) -> None:
    got = extract_filters_from_text(query)
    assert got.min_age == min_age
    assert got.max_age == max_age


@pytest.mark.parametrize(
    ("query", "min_year", "max_year"),
    [
        ("after 2020", 2020, None),
        ("since 2018", 2018, None),
        ("from 2015", 2015, None),
        ("before 2010", None, 2010),
    ],
)
def test_year(query: str, min_year: int | None, max_year: int | None) -> None:
    got = extract_filters_from_text(query)
    assert got.min_year == min_year
    assert got.max_year == max_year


@pytest.mark.parametrize(
    ("query", "name"),
    [
        ("games like Catan", "Catan"),
        ("game like Catan", "Catan"),
        ("something like Catan", "Catan"),
        ("similar to Ticket to Ride", "Ticket to Ride"),
        ("alternatives to Wingspan", "Wingspan"),
        ("alternative to Wingspan", "Wingspan"),
        ('like "Catan"', "Catan"),
    ],
)
def test_similar_to_triggers(query: str, name: str) -> None:
    got = extract_filters_from_text(query)
    assert got.similar_to == name
    assert got.keywords is None or name.lower() not in " ".join(got.keywords).lower()


def test_similar_to_stops_before_player_filter() -> None:
    got = extract_filters_from_text("like Ticket to Ride for 4")
    assert got.similar_to == "Ticket to Ride"
    assert got.player_count == 4


def test_like_category_alias_is_not_similar_to() -> None:
    got = extract_filters_from_text("I like strategy games")
    assert got.similar_to is None
    assert got.categories == ["Strategy"]


def test_combined_short_query() -> None:
    got = extract_filters_from_text(
        "light strategy game for 4 players under 60 minutes"
    )
    assert got.player_count == 4
    assert got.max_play_time_minutes == 60
    assert got.complexity == "light"
    assert got.categories == ["Strategy"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_text_extractor.py -v`

Expected: FAIL (`extract_filters_from_text` not defined)

- [ ] **Step 3: Write the implementation**

Replace `app/text_extractor.py` with:

```python
from __future__ import annotations

import re

from app.models import ExtractedFilters

_SENTENCE_SPLIT = re.compile(r"[.?!]+")

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
    rf"\b(?:for\s+)?({_NUM})(?:\s*-\s*|\s+)?players?\b|\b({_NUM})-player\b",
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


def extract_filters_from_text(query: str) -> ExtractedFilters:
    text = query.strip()
    if not text:
        return ExtractedFilters()

    used: list[tuple[int, int]] = []
    data: dict = {}

    def _take(match: re.Match | None, *group_indexes: int):
        if match is None:
            return None
        used.append(match.span())
        for i in group_indexes:
            raw = match.group(i)
            if raw is not None:
                return raw
        return None

    best = _BEST_RE.search(text)
    rec = _REC_RE.search(text)
    if best:
        data["best_with_player_count"] = _parse_num(_take(best, 1))
    if rec:
        data["recommended_with_player_count"] = _parse_num(_take(rec, 1))
    if "best_with_player_count" not in data and "recommended_with_player_count" not in data:
        player = _PLAYER_RE.search(text)
        if player:
            data["player_count"] = _parse_num(_take(player, 1, 2))

    if _HOUR_RE.search(text):
        hour = _HOUR_RE.search(text)
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
    for m in re.finditer(r"[.?!]", text):
        if m.start() >= name_start:
            stops.append(m.start())
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
```

**Fixes if tests fail (apply in this task, do not skip):**

- `"crunchy euro"`: leftover `euro` may appear in `keywords`; tests only assert `complexity`.
- `"moderate weight"`: `\bmedium\b` must not steal this; `moderate` is in `_MEDIUM`.
- `"hard core game"`: phrase `hard core` must be tried before `\bhard\b` (already sorted by length in `_phrase_regex`).
- `like Ticket to Ride for 4`: `_PLAYER_RE` span must be in `used` **before** `_extract_similar_to` so the name stops at `for 4`.
- `I like strategy games`: bare `like` + category alias → `similar_to` null; `strategy` still fills `categories`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_text_extractor.py -v`

Expected: PASS. If `keywords` on similar-to tests include the game name, strip those tokens in `_extract_similar_to` by including the full trigger+name span in `used` (already `(match.start(), name_end)`).

- [ ] **Step 5: Commit**

```bash
git add app/text_extractor.py tests/test_text_extractor.py
git commit -m "$(cat <<'EOF'
feat: extract recommend filters from query text

EOF
)"
```

---

### Task 3: `should_use_llm` and `resolve_filters`

**Files:**
- Modify: `app/query_extractor.py`
- Create: `tests/test_resolve_filters.py`

**Interfaces:**
- Consumes: `extract_filters_from_text`, `sentence_count`, `has_active_hard_filters`, existing `extract_filters`
- Produces:
  - `should_use_llm(query: str, text_filters: ExtractedFilters) -> bool`
  - `resolve_filters(llm: BaseChatModel | None, query: str) -> ExtractedFilters`

`should_use_llm` is true iff `sentence_count(query) > 3` **or** (`text_filters.similar_to` is null **and** `not has_active_hard_filters(text_filters)`).

Catch `APIConnectionError`, `APIStatusError`, and any other exception from `extract_filters`. Log warning with `type(exc).__name__`. Return text filters. Log `extraction_source=text|llm` and whether fallback was attempted.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_resolve_filters.py`:

```python
from unittest.mock import MagicMock, patch

import pytest
from openai import APIConnectionError

from app.models import ExtractedFilters
from app.query_extractor import resolve_filters, should_use_llm


def test_should_use_llm_false_for_short_hard_filters() -> None:
    filters = ExtractedFilters(player_count=4)
    assert should_use_llm("for 4 players", filters) is False


def test_should_use_llm_false_for_short_similar_to() -> None:
    filters = ExtractedFilters(similar_to="Catan")
    assert should_use_llm("games like Catan", filters) is False


def test_should_use_llm_true_when_empty() -> None:
    assert should_use_llm("something fun tonight", ExtractedFilters()) is True


def test_should_use_llm_true_when_over_three_sentences() -> None:
    query = "a. b. c. d"
    filters = ExtractedFilters(player_count=4, similar_to="Catan")
    assert should_use_llm(query, filters) is True


@patch("app.query_extractor.extract_filters")
def test_resolve_skips_llm_when_hard_filters(mock_llm: MagicMock) -> None:
    result = resolve_filters(MagicMock(), "for 4 players")
    mock_llm.assert_not_called()
    assert result.player_count == 4


@patch("app.query_extractor.extract_filters")
def test_resolve_skips_llm_when_similar_to(mock_llm: MagicMock) -> None:
    result = resolve_filters(MagicMock(), "games like Catan")
    mock_llm.assert_not_called()
    assert result.similar_to == "Catan"


@patch("app.query_extractor.extract_filters")
def test_resolve_uses_llm_when_no_text_signal(mock_llm: MagicMock) -> None:
    mock_llm.return_value = ExtractedFilters(keywords=["fun"])
    llm = MagicMock()
    result = resolve_filters(llm, "something fun tonight")
    mock_llm.assert_called_once()
    assert result.keywords == ["fun"]


@patch("app.query_extractor.extract_filters")
def test_resolve_uses_llm_on_long_query_even_with_filters(
    mock_llm: MagicMock,
) -> None:
    mock_llm.return_value = ExtractedFilters(player_count=2)
    result = resolve_filters(MagicMock(), "a. b. c. d for 4 players")
    mock_llm.assert_called_once()
    assert result.player_count == 2


@patch("app.query_extractor.extract_filters")
def test_resolve_keeps_text_when_llm_raises(mock_llm: MagicMock) -> None:
    mock_llm.side_effect = APIConnectionError(request=MagicMock())
    result = resolve_filters(MagicMock(), "something fun tonight")
    assert result == ExtractedFilters() or result.player_count is None


def test_resolve_skips_llm_when_llm_none_and_fallback_indicated() -> None:
    result = resolve_filters(None, "something fun tonight")
    assert result.player_count is None
```

If `APIConnectionError(request=...)` fails to construct in this project's openai version, use `side_effect = RuntimeError("provider")` instead; `resolve_filters` must catch that too.

For `test_resolve_keeps_text_when_llm_raises`, assert `mock_llm` was called and the return value equals `extract_filters_from_text("something fun tonight")`:

```python
from app.text_extractor import extract_filters_from_text

assert result == extract_filters_from_text("something fun tonight")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_resolve_filters.py -v`

Expected: FAIL (`should_use_llm` / `resolve_filters` not defined)

- [ ] **Step 3: Implement orchestration**

Add to `app/query_extractor.py` (keep `EXTRACTION_PROMPT` and `extract_filters` unchanged). New imports:

```python
from langchain_core.language_models import BaseChatModel
from openai import APIConnectionError, APIStatusError

from app.llm_parsing import invoke_structured
from app.models import ExtractedFilters
from app.sql_filters import has_active_hard_filters
from app.text_extractor import extract_filters_from_text, sentence_count
```

Append:

```python
def should_use_llm(query: str, text_filters: ExtractedFilters) -> bool:
    if sentence_count(query) > 3:
        return True
    if text_filters.similar_to:
        return False
    return not has_active_hard_filters(text_filters)


def resolve_filters(llm: BaseChatModel | None, query: str) -> ExtractedFilters:
    text_filters = extract_filters_from_text(query)
    logger.info("Text filters: %s", text_filters.model_dump())
    fallback = should_use_llm(query, text_filters)
    if not fallback:
        logger.info("extraction_source=text fallback_attempted=false")
        return text_filters
    if llm is None:
        logger.warning("LLM fallback indicated but llm is None; using text filters")
        logger.info("extraction_source=text fallback_attempted=true")
        return text_filters
    try:
        llm_filters = extract_filters(llm, query)
        logger.info("extraction_source=llm fallback_attempted=true")
        return llm_filters
    except (APIConnectionError, APIStatusError, Exception) as exc:
        logger.warning(
            "LLM extraction failed (%s); keeping text filters",
            type(exc).__name__,
        )
        logger.info("extraction_source=text fallback_attempted=true")
        return text_filters
```

Keep the `except Exception` so JSON/parse/validation failures fall back to text. Do not merge LLM fields into text.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_resolve_filters.py tests/test_text_extractor.py -v`

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/query_extractor.py tests/test_resolve_filters.py
git commit -m "$(cat <<'EOF'
feat: fall back to LLM extraction only when text is sparse or long

EOF
)"
```

---

### Task 4: Wire `/recommend`

**Files:**
- Modify: `app/main.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- Consumes: `resolve_filters(llm, query)`
- Produces: `/recommend` uses text-first filters; extraction never 502s; synthesis still 502s if `llm is None` or provider errors

Remove the pre-extract `if llm is None: raise 502`. Remove the `try/except` around extraction that maps provider errors to 502. Keep that pattern around `synthesize_recommendations`. If `llm is None` at synthesis, raise 502.

- [ ] **Step 1: Write the failing API tests**

In `tests/test_api.py`, change existing recommend tests to patch `app.main.resolve_filters` instead of `app.main.extract_filters`. Rename the mock parameter to `mock_resolve`.

Add:

```python
@patch("app.main.synthesize_recommendations")
@patch("app.query_extractor.extract_filters")
def test_recommend_text_path_does_not_call_llm_extract(
    mock_extract: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_extract.side_effect = AssertionError("LLM extract should not run")
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[
            GameRecommendation(
                name="Catan",
                reason="Fits player count.",
                min_players=3,
                max_players=4,
                play_time_minutes=90,
                categories=["strategy"],
            )
        ],
        reasoning="ok",
    )
    response = client.post("/recommend", json={"query": "for 4 players"})
    assert response.status_code == 200
    assert response.json()["filters_applied"]["player_count"] == 4
    mock_extract.assert_not_called()


@patch("app.main.synthesize_recommendations")
def test_recommend_extraction_survives_missing_llm(
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_synthesize.return_value = SynthesisOutput(
        recommendations=[],
        reasoning="none",
    )
    app_state.llm = None
    response = client.post("/recommend", json={"query": "for 4 players"})
    assert response.status_code == 200
    assert response.json()["filters_applied"]["player_count"] == 4
    app_state.llm = MagicMock()
```

The last line restores a dummy llm so later tests in the same process are not stuck. If other tests depend on the real llm from lifespan, save and restore:

```python
previous = app_state.llm
app_state.llm = None
try:
    response = client.post(...)
    assert response.status_code == 200
finally:
    app_state.llm = previous
```

If synthesis is called with `llm=None` and the mock still runs, 200 is correct. Do **not** add a 502 on missing llm before `resolve_filters`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_api.py -v`

Expected: FAIL (`resolve_filters` not imported / extract still 502s / still calling `extract_filters` from `main`)

- [ ] **Step 3: Wire `main.py`**

Change import:

```python
from app.query_extractor import resolve_filters
```

Replace the recommend extraction block with:

```python
    settings = app_state.settings
    llm = app_state.llm

    filters = resolve_filters(llm, request.query)

    chroma_dir = Path(settings.chroma_persist_dir)
    vector_store = get_vector_store(chroma_dir, get_embeddings(settings))
    session_factory = get_session_factory()
    with session_factory() as session:
        candidates, filters_relaxed = retrieve_games(
            session, vector_store, filters, request.query, top_k=5
        )

    if llm is None:
        raise HTTPException(status_code=502, detail={"error": "LLM unavailable"})

    try:
        synthesis = synthesize_recommendations(llm, request.query, filters, candidates)
```

`test_recommend_extraction_survives_missing_llm` mocks `synthesize_recommendations` **before** the `llm is None` check would 502. **Move the `llm is None` 502 to immediately before synthesize, after retrieval**, so extraction + retrieval can run without an LLM. That matches the spec (no 502 at extraction). Synthesis still 502s when unmocked and `llm is None`.

Update the two existing tests:

```python
@patch("app.main.synthesize_recommendations")
@patch("app.main.resolve_filters")
def test_recommend_response_shape(
    mock_resolve: MagicMock,
    mock_synthesize: MagicMock,
    client: TestClient,
) -> None:
    mock_resolve.return_value = ExtractedFilters(
        player_count=4,
        categories=["strategy"],
        max_play_time_minutes=60,
        complexity="light",
    )
    # ... remainder unchanged except mock_extract → mock_resolve
```

Same for `test_recommend_filters_applied_includes_similar_to`. Leave `test_recommend_no_games_indexed_returns_503` patching `resolve_filters` (or keep both patches unused).

- [ ] **Step 4: Run tests**

Run: `pytest tests/test_api.py tests/test_resolve_filters.py tests/test_text_extractor.py tests/test_llm_parsing.py tests/test_sql_filters.py -v`

Expected: PASS. `has_active_hard_filters(ExtractedFilters(similar_to="Catan")) is False` still holds.

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_api.py
git commit -m "$(cat <<'EOF'
feat: use text extraction on recommend before LLM fallback

EOF
)"
```

---

## Self-review

| Spec requirement | Task |
|------------------|------|
| `sentence_count` on `. ? !`, empty chunks | 1 |
| Regex extractor, all fields including similar_to + complexity synonyms | 2 |
| Skip LLM if similar_to or hard filters; LLM if >3 sentences or neither | 3 |
| LLM replaces text; failure/`None` keeps text | 3 |
| No extraction 502; synthesis 502 unchanged | 4 |
| `HARD_FILTER_FIELDS` unchanged | 4 (assert via existing sql test) |
| Logs `extraction_source` + fallback | 3 |

No TBD/TODO placeholders. Types: `resolve_filters(llm: BaseChatModel | None, query: str) -> ExtractedFilters` used in Task 3 and Task 4.
