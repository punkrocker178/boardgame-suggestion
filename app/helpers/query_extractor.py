import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from openai import APIConnectionError, APIStatusError

from app.api.models import ExtractedFilters
from app.helpers.llm_parsing import invoke_structured
from app.helpers.sql_filters import has_active_hard_filters
from app.helpers.text_extractor import extract_filters_from_text, sanitize_gibberish, sentence_count

logger = logging.getLogger(__name__)


EXTRACTION_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Extract structured board game search filters from the user query. "
            "Only set fields explicitly mentioned or clearly implied. "
            "Leave unmentioned fields as null. Do not guess.\n\n"
            "Complexity vs weight:\n"
            "- Qualitative words (light, medium, heavy, easy, complex) → set complexity; "
            "leave min_weight/max_weight null unless the user also states a numeric weight.\n"
            "- Numeric BGG weight language (weight > 3, at least 3.5, heavier than 2) → "
            "set min_weight and/or max_weight; leave complexity null unless they also used "
            "bucket words.\n\n"
            "Age:\n"
            "- '14+', 'adults' → min_age\n"
            "- 'for my 8-year-old' / kid age → max_age\n\n"
            "Year: 'after 2020' → min_year; 'before 2010' → max_year.\n\n"
            "Players:\n"
            "- Plain 'for 4 players' → player_count only.\n"
            "- 'best with 4' → best_with_player_count=4 (do not set from plain player_count).\n"
            "- 'recommended for 3' / clear BGG poll language → recommended_with_player_count; "
            "otherwise use player_count only.\n\n"
            "Categories: prefer BoardGameGeek-style names (e.g. Strategy, Party, "
            "Card Game). Unknown labels may be treated as keywords later.\n\n"
            "Similar-to:\n"
            "- 'games like Catan', 'similar to Ticket to Ride', 'alternatives to Wingspan' "
            "→ similar_to = the game name only.\n"
            "- Only set similar_to when a name is explicit. Do not guess.\n"
            "- Do not put that name in keywords when similar_to is set.\n\n"
            "Return JSON with exactly these fields:\n"
            "- player_count (int or null)\n"
            "- categories (list of strings or null)\n"
            "- max_play_time_minutes (int or null)\n"
            "- complexity ('light', 'medium', 'heavy', or null)\n"
            "- min_weight (float or null)\n"
            "- max_weight (float or null)\n"
            "- min_age (int or null)\n"
            "- max_age (int or null)\n"
            "- min_year (int or null)\n"
            "- max_year (int or null)\n"
            "- best_with_player_count (int or null)\n"
            "- recommended_with_player_count (int or null)\n"
            "- keywords (list of strings or null)\n"
            "- similar_to (string game name or null)",
        ),
        ("human", "{query}"),
    ]
)


def extract_filters(llm: BaseChatModel, query: str) -> ExtractedFilters:
    logger.info("Extracting filters from query: %r", query)
    filters = invoke_structured(llm, EXTRACTION_PROMPT, ExtractedFilters, {"query": query})
    logger.info("Extracted filters: %s", filters.model_dump())
    return filters


def should_use_llm(query: str, text_filters: ExtractedFilters) -> bool:
    if not query.strip():
        return False
    if sentence_count(query) > 3:
        return True
    if text_filters.similar_to:
        return False
    return not has_active_hard_filters(text_filters)


def resolve_filters(llm: BaseChatModel | None, query: str) -> ExtractedFilters:
    working = sanitize_gibberish(query)
    text_filters = extract_filters_from_text(working)
    logger.info("Text filters: %s", text_filters.model_dump())
    fallback = should_use_llm(working, text_filters)
    if not fallback:
        logger.info("extraction_source=text fallback_attempted=false")
        return text_filters
    if llm is None:
        logger.warning("LLM fallback indicated but llm is None; using text filters")
        logger.info("extraction_source=text fallback_attempted=true")
        return text_filters
    try:
        llm_filters = extract_filters(llm, working)
        logger.info("extraction_source=llm fallback_attempted=true")
        return llm_filters
    except (APIConnectionError, APIStatusError, Exception) as exc:
        logger.warning(
            "LLM extraction failed (%s); keeping text filters",
            type(exc).__name__,
        )
        logger.info("extraction_source=text fallback_attempted=true")
        return text_filters
