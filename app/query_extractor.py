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
            "Return JSON with exactly these fields:\n"
            "- player_count (int or null)\n"
            "- categories (list of strings or null)\n"
            "- max_play_time_minutes (int or null)\n"
            "- complexity ('light', 'medium', 'heavy', or null)\n"
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
