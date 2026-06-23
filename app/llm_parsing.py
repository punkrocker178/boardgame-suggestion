import json
import logging
import re
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)

_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?```", re.DOTALL | re.IGNORECASE)


def strip_json_fences(text: str) -> str:
    text = text.strip()
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1).strip()
    return text


def parse_model_response(text: str, model: type[T]) -> T:
    logger.debug("Parsing %s response (%d chars)", model.__name__, len(text))
    payload = json.loads(strip_json_fences(text))
    return model.model_validate(payload)


def invoke_structured(
    llm: BaseChatModel,
    prompt: ChatPromptTemplate,
    model: type[T],
    variables: dict,
) -> T:
    logger.debug("Invoking structured LLM call for %s variables=%s", model.__name__, list(variables))
    try:
        structured_llm = llm.with_structured_output(model)
        result = (prompt | structured_llm).invoke(variables)
        if isinstance(result, model):
            logger.debug("Structured output succeeded for %s", model.__name__)
            return result
        parsed = model.model_validate(result)
        logger.debug("Structured output validated for %s", model.__name__)
        return parsed
    except Exception as exc:
        logger.warning(
            "Structured output failed for %s (%s); falling back to manual JSON parse",
            model.__name__,
            type(exc).__name__,
        )
        response = (prompt | llm).invoke(variables)
        content = _message_content(response)
        logger.debug("Raw LLM response for %s: %s", model.__name__, content)
        return parse_model_response(content, model)


def _message_content(message: BaseMessage | str) -> str:
    if isinstance(message, str):
        return message
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)
