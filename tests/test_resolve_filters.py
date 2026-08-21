from unittest.mock import MagicMock, patch

from openai import APIConnectionError

from app.models import ExtractedFilters
from app.query_extractor import resolve_filters, should_use_llm
from app.text_extractor import extract_filters_from_text


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
    query = "something fun tonight"
    result = resolve_filters(MagicMock(), query)
    assert result == extract_filters_from_text(query)


def test_resolve_skips_llm_when_llm_none_and_fallback_indicated() -> None:
    result = resolve_filters(None, "something fun tonight")
    assert result.player_count is None
