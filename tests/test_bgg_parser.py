from app.bgg.parser import (
    complexity_from_weight,
    parse_player_count_summary,
    parse_thing_response,
    resolve_play_time,
    strip_html,
)
from tests.fixtures.bgg_thing import EMPTY_PLAYTIME_XML, SAMPLE_THING_XML


def test_strip_html_removes_tags() -> None:
    assert strip_html("<b>Hello</b> &amp; world") == "Hello & world"


def test_resolve_play_time_prefers_playing_time() -> None:
    assert resolve_play_time(120, 60, 90) == 120


def test_resolve_play_time_falls_back_to_max_min_playtime() -> None:
    assert resolve_play_time(0, 45, 90) == 90
    assert resolve_play_time(None, 0, 0) is None


def test_complexity_from_weight() -> None:
    assert complexity_from_weight(1.5) == "light"
    assert complexity_from_weight(2.5) == "medium"
    assert complexity_from_weight(4.0) == "heavy"
    assert complexity_from_weight(None) is None


def test_parse_thing_response() -> None:
    parsed = parse_thing_response(SAMPLE_THING_XML)
    assert 224517 in parsed

    game = parsed[224517]
    assert game.min_players == 2
    assert game.max_players == 4
    assert game.playing_time == 120
    assert game.weight == 3.86
    assert game.categories == [(1021, "Economic"), (1086, "Territory Building")]
    assert game.mechanics == [(2081, "Route/Network Building")]
    assert game.best_with_players == [4, 5]
    assert game.recommended_with_players == [3, 4, 5, 6]
    assert game.thumbnail_url == "https://example.com/thumb.jpg"


def test_parse_thing_response_playtime_fallback() -> None:
    parsed = parse_thing_response(EMPTY_PLAYTIME_XML)
    game = parsed[999]
    assert game.playing_time == 90


def test_parse_player_count_summary_variants() -> None:
    assert parse_player_count_summary("Best with 4–5 players") == [4, 5]
    assert parse_player_count_summary("Best with 4-5 players") == [4, 5]
    assert parse_player_count_summary("Recommended with 2–4 players") == [2, 3, 4]
    assert parse_player_count_summary("Best with 2, 4 players") == [2, 4]
    assert parse_player_count_summary("Best with 4 players") == [4]
    assert parse_player_count_summary("Recommended with 7+ players") is None
    assert parse_player_count_summary("") is None
    assert parse_player_count_summary(None) is None


def test_parse_skips_links_missing_id() -> None:
    xml = """<?xml version="1.0"?>
    <items><item type="boardgame" id="1">
      <link type="boardgamecategory" value="Economic"/>
      <link type="boardgamecategory" id="1021" value="Economic"/>
    </item></items>"""
    game = parse_thing_response(xml)[1]
    assert game.categories == [(1021, "Economic")]
