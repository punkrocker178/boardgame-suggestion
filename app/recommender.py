import logging

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.llm_parsing import invoke_structured
from app.models import ExtractedFilters, FiltersApplied, GameRecommendation

logger = logging.getLogger(__name__)


class SynthesisOutput(BaseModel):
    recommendations: list[GameRecommendation] = Field(max_length=3)
    reasoning: str


SYNTHESIS_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You are a friendly board game game master. "
            "Recommend up to 3 games from the candidates that best match the user's request. "
            "Provide a short reason for each game and overall reasoning.\n\n"
            "Return JSON with:\n"
            "- recommendations: list of up to 3 objects with name, reason, min_players, "
            "max_players, play_time_minutes, categories\n"
            "- reasoning: string",
        ),
        (
            "human",
            "User query: {query}\n\n"
            "Extracted filters: {filters}\n\n"
            "Candidate games:\n{candidates}",
        ),
    ]
)


def _format_candidates(documents: list[Document]) -> str:
    lines: list[str] = []
    for doc in documents:
        meta = doc.metadata
        categories = meta.get("categories", "")
        if isinstance(categories, str):
            categories = [c.strip() for c in categories.split(",") if c.strip()]
        complexity = meta.get("complexity", "unknown")
        lines.append(
            f"- {meta['name']}: {doc.page_content} "
            f"Players {meta['min_players']}-{meta['max_players']}, "
            f"{meta['play_time_minutes']} min, complexity {complexity}, "
            f"categories {', '.join(categories)}"
        )
    return "\n".join(lines)


def synthesize_recommendations(
    llm: BaseChatModel,
    query: str,
    filters: ExtractedFilters,
    candidates: list[Document],
) -> SynthesisOutput:
    candidate_names = [doc.metadata.get("name") for doc in candidates]
    logger.info(
        "Synthesizing recommendations for query=%r from %d candidates: %s",
        query,
        len(candidates),
        candidate_names,
    )
    result = invoke_structured(
        llm.bind(temperature=0.3),
        SYNTHESIS_PROMPT,
        SynthesisOutput,
        {
            "query": query,
            "filters": filters.model_dump_json(),
            "candidates": _format_candidates(candidates),
        },
    )
    logger.info(
        "Synthesis complete: %d recommendations — %s",
        len(result.recommendations),
        [rec.name for rec in result.recommendations],
    )
    logger.debug("Synthesis reasoning: %s", result.reasoning)
    return result


def filters_to_applied(filters: ExtractedFilters) -> FiltersApplied:
    return FiltersApplied(
        player_count=filters.player_count,
        categories=filters.categories,
        max_play_time_minutes=filters.max_play_time_minutes,
        complexity=filters.complexity,
        keywords=filters.keywords,
    )
