import logging

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate

from app.llm_parsing import invoke_structured
from app.models import ExtractedFilters

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
            "- keywords (list of strings or null)",
        ),
        ("human", "{query}"),
    ]
)


def extract_filters(llm: BaseChatModel, query: str) -> ExtractedFilters:
    logger.info("Extracting filters from query: %r", query)
    filters = invoke_structured(llm, EXTRACTION_PROMPT, ExtractedFilters, {"query": query})
    logger.info("Extracted filters: %s", filters.model_dump())
    return filters
