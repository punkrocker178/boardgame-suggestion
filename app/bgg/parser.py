import html
import re
from dataclasses import dataclass, field
from typing import Literal
from xml.etree import ElementTree as ET

Complexity = Literal["light", "medium", "heavy"]

_EN_DASH = "\u2013"
_RANGE_RE = re.compile(rf"(\d+)\s*(?:{_EN_DASH}|-)\s*(\d+)")


@dataclass
class BggThingData:
    id: int
    description: str | None = None
    min_players: int | None = None
    max_players: int | None = None
    playing_time: int | None = None
    min_play_time: int | None = None
    max_play_time: int | None = None
    min_age: int | None = None
    weight: float | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None
    categories: list[tuple[int, str]] = field(default_factory=list)
    mechanics: list[tuple[int, str]] = field(default_factory=list)
    best_with_players: list[int] | None = None
    recommended_with_players: list[int] | None = None


_HTML_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str | None) -> str | None:
    if not text:
        return None
    unescaped = html.unescape(text)
    cleaned = _HTML_TAG_RE.sub(" ", unescaped)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned or None


def _int_or_none(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except ValueError:
        return None
    return parsed


def _float_or_none(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _child_text(item: ET.Element, tag: str) -> str | None:
    child = item.find(tag)
    if child is None:
        return None
    if "value" in child.attrib:
        return child.attrib["value"].strip()
    if child.text is None:
        return None
    return child.text.strip()


def _child_int(item: ET.Element, tag: str) -> int | None:
    return _int_or_none(_child_text(item, tag))


def resolve_play_time(
    playing_time: int | None,
    min_play_time: int | None,
    max_play_time: int | None,
) -> int | None:
    if playing_time and playing_time > 0:
        return playing_time
    candidates = [value for value in (min_play_time, max_play_time) if value and value > 0]
    if not candidates:
        return None
    return max(candidates)


def complexity_from_weight(weight: float | None) -> Complexity | None:
    if weight is None:
        return None
    if weight < 2.0:
        return "light"
    if weight < 3.5:
        return "medium"
    return "heavy"


def parse_player_count_summary(value: str | None) -> list[int] | None:
    if not value or not value.strip():
        return None
    # Drop N+ tokens (e.g. "7+") — do not invent an open-ended count
    cleaned = re.sub(r"\d+\s*\+", " ", value)
    numbers: set[int] = set()
    for match in _RANGE_RE.finditer(cleaned):
        low, high = int(match.group(1)), int(match.group(2))
        if low < 1 or low > high:
            continue
        numbers.update(range(low, high + 1))
    without_ranges = _RANGE_RE.sub(" ", cleaned)
    for match in re.finditer(r"\d+", without_ranges):
        n = int(match.group(0))
        if n >= 1:
            numbers.add(n)
    if not numbers:
        return None
    return sorted(numbers)


def _parse_item(item: ET.Element) -> BggThingData:
    game_id = int(item.attrib["id"])
    min_play_time = _child_int(item, "minplaytime")
    max_play_time = _child_int(item, "maxplaytime")
    playing_time_raw = _child_int(item, "playingtime")

    weight_el = item.find("./statistics/ratings/averageweight")
    weight = None
    if weight_el is not None:
        weight = _float_or_none(
            weight_el.attrib.get("value") if "value" in weight_el.attrib else weight_el.text
        )

    categories: list[tuple[int, str]] = []
    mechanics: list[tuple[int, str]] = []
    for link in item.findall("link"):
        link_type = link.attrib.get("type")
        value = link.attrib.get("value")
        raw_id = link.attrib.get("id")
        if not value or not raw_id:
            continue
        link_id = _int_or_none(raw_id)
        if link_id is None:
            continue
        if link_type == "boardgamecategory":
            categories.append((link_id, value))
        elif link_type == "boardgamemechanic":
            mechanics.append((link_id, value))

    best_with = None
    recommended_with = None
    summary = item.find("poll-summary[@name='suggested_numplayers']")
    if summary is not None:
        for result in summary.findall("result"):
            name = result.attrib.get("name")
            parsed = parse_player_count_summary(result.attrib.get("value"))
            if name == "bestwith":
                best_with = parsed
            elif name == "recommmendedwith":
                recommended_with = parsed

    return BggThingData(
        id=game_id,
        description=strip_html(_child_text(item, "description")),
        min_players=_child_int(item, "minplayers"),
        max_players=_child_int(item, "maxplayers"),
        playing_time=resolve_play_time(playing_time_raw, min_play_time, max_play_time),
        min_play_time=min_play_time,
        max_play_time=max_play_time,
        min_age=_child_int(item, "minage"),
        weight=weight,
        thumbnail_url=_child_text(item, "thumbnail"),
        image_url=_child_text(item, "image"),
        categories=categories,
        mechanics=mechanics,
        best_with_players=best_with,
        recommended_with_players=recommended_with,
    )


def parse_thing_response(xml_text: str) -> dict[int, BggThingData]:
    root = ET.fromstring(xml_text)
    results: dict[int, BggThingData] = {}
    for item in root.findall("item"):
        parsed = _parse_item(item)
        results[parsed.id] = parsed
    return results
