import pytest

from app.api.models import ExtractedFilters
from app.helpers.text_extractor import extract_filters_from_text, sanitize_gibberish, sentence_count


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


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("asdf for 4 players", "for 4 players"),
        ("xxxx", ""),
        ("aaaabcd", ""),
        ("something fun tonight", "something fun tonight"),
        ("asdf. asdf. asdf. asdf. for 4 players", "for 4 players"),
        ("rhythm", "rhythm"),
        ("  asdf  tsk  ", ""),
    ],
)
def test_sanitize_gibberish(query: str, expected: str) -> None:
    assert sanitize_gibberish(query) == expected


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
