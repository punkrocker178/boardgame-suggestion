import pytest
from pydantic import ValidationError

from app.helpers.llm_parsing import (
    _recover_from_validation_error,
    parse_model_response,
    strip_json_fences,
)
from app.api.models import ExtractedFilters
from app.services.recommender import SynthesisOutput


def test_strip_json_fences() -> None:
    raw = '```json\n{"player_count": 4}\n```'
    assert strip_json_fences(raw) == '{"player_count": 4}'


def test_parse_model_response_from_fenced_json() -> None:
    raw = '```json\n{"player_count": 4, "complexity": "light"}\n```'
    filters = parse_model_response(raw, ExtractedFilters)
    assert filters.player_count == 4
    assert filters.complexity == "light"


def test_extracted_filters_normalizes_nested_play_time() -> None:
    filters = ExtractedFilters.model_validate(
        {"complexity": "light", "play_time": {"max": 60}, "player_count": 4}
    )
    assert filters.max_play_time_minutes == 60


def test_extracted_filters_normalizes_max_alias() -> None:
    filters = ExtractedFilters.model_validate(
        {"complexity": "light", "max": 60, "player_count": 4}
    )
    assert filters.max_play_time_minutes == 60


def test_extracted_filters_rejects_invalid_complexity() -> None:
    with pytest.raises(Exception):
        ExtractedFilters.model_validate({"complexity": "extreme"})


def test_extracted_filters_similar_to_round_trip() -> None:
    filters = ExtractedFilters.model_validate({"similar_to": "Catan"})
    assert filters.similar_to == "Catan"


def test_extracted_filters_blank_similar_to_is_none() -> None:
    filters = ExtractedFilters.model_validate({"similar_to": "  "})
    assert filters.similar_to is None


def test_recover_from_validation_error_fenced_json() -> None:
    raw = (
        "**Recommendations**\n\n```json\n"
        '{"recommendations": [{"name": "Clank!", "reason": "fun", '
        '"min_players": 2, "max_players": 4, "play_time_minutes": 60, '
        '"categories": ["deck building"]}], "reasoning": "great picks"}\n'
        "```"
    )
    with pytest.raises(ValidationError) as raised:
        SynthesisOutput.model_validate_json(raw)
    recovered = _recover_from_validation_error(raised.value, SynthesisOutput)
    assert recovered is not None
    assert recovered.recommendations[0].name == "Clank!"
    assert recovered.reasoning == "great picks"
