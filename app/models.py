from typing import Literal

from pydantic import BaseModel, Field, model_validator


class RecommendRequest(BaseModel):
    query: str = Field(min_length=1)
    session_id: str | None = None


class GameRecommendation(BaseModel):
    name: str
    reason: str
    min_players: int
    max_players: int
    play_time_minutes: int
    categories: list[str]


class FiltersApplied(BaseModel):
    player_count: int | None = None
    categories: list[str] | None = None
    max_play_time_minutes: int | None = None
    complexity: Literal["light", "medium", "heavy"] | None = None
    min_weight: float | None = None
    max_weight: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    best_with_player_count: int | None = None
    recommended_with_player_count: int | None = None
    keywords: list[str] | None = None
    similar_to: str | None = None


class RecommendResponse(BaseModel):
    recommendations: list[GameRecommendation]
    reasoning: str
    filters_applied: FiltersApplied
    filters_relaxed: bool = False


class ExtractedFilters(BaseModel):
    player_count: int | None = None
    categories: list[str] | None = None
    max_play_time_minutes: int | None = None
    complexity: Literal["light", "medium", "heavy"] | None = None
    min_weight: float | None = None
    max_weight: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    best_with_player_count: int | None = None
    recommended_with_player_count: int | None = None
    keywords: list[str] | None = None
    similar_to: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_loose_schema(cls, data: object) -> object:
        if not isinstance(data, dict):
            return data

        if data.get("max_play_time_minutes") is None:
            play_time = data.get("play_time") or data.get("playtime") or data.get("duration")
            if isinstance(play_time, dict):
                data["max_play_time_minutes"] = play_time.get("max") or play_time.get(
                    "max_minutes"
                )
            elif isinstance(play_time, (int, float)):
                data["max_play_time_minutes"] = int(play_time)
            elif data.get("max") is not None:
                data["max_play_time_minutes"] = data.pop("max")

        if data.get("player_count") is None:
            players = data.get("players")
            if isinstance(players, dict):
                data["player_count"] = (
                    players.get("exact")
                    or players.get("count")
                    or players.get("num")
                    or players.get("min")
                )
            elif isinstance(players, (int, float)):
                data["player_count"] = int(players)

        categories = data.get("categories")
        if isinstance(categories, str):
            data["categories"] = [c.strip() for c in categories.split(",") if c.strip()]

        similar_to = data.get("similar_to")
        if isinstance(similar_to, str) and not similar_to.strip():
            data["similar_to"] = None

        return data


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    indexed_games: int


class SearchRequest(BaseModel):
    q: str | None = None
    limit: int = Field(default=20, ge=1, le=50)
    cursor: str | None = None
    player_count: int | None = None
    categories: list[str] | None = None
    max_play_time_minutes: int | None = None
    complexity: Literal["light", "medium", "heavy"] | None = None
    min_weight: float | None = None
    max_weight: float | None = None
    min_age: int | None = None
    max_age: int | None = None
    min_year: int | None = None
    max_year: int | None = None
    best_with_player_count: int | None = None
    recommended_with_player_count: int | None = None


class SearchGame(BaseModel):
    id: int
    name: str
    year_published: int | None
    rank: int | None
    is_expansion: bool
    min_players: int | None
    max_players: int | None
    playing_time: int | None
    min_age: int | None
    weight: float | None
    thumbnail_url: str | None
    categories: list[str]


class SearchResponse(BaseModel):
    items: list[SearchGame]
    next_cursor: str | None


class AutocompleteGame(BaseModel):
    id: int
    name: str
    year_published: int | None


class AutocompleteResponse(BaseModel):
    suggestions: list[AutocompleteGame]
