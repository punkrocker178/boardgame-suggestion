import json
import logging
import re
from typing import TypeVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import BaseMessage
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, ValidationError

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


def _try_parse_fenced_response(text: str, model: type[T]) -> T | None:
    try:
        return parse_model_response(text, model)
    except (json.JSONDecodeError, ValidationError):
        return None


def _recover_from_validation_error(exc: ValidationError, model: type[T]) -> T | None:
    for err in exc.errors():
        if err.get("type") != "json_invalid":
            continue
        raw = err.get("input")
        if isinstance(raw, str):
            parsed = _try_parse_fenced_response(raw, model)
            if parsed is not None:
                return parsed
    return None


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
        if isinstance(result, str):
            parsed = _try_parse_fenced_response(result, model)
            if parsed is not None:
                logger.debug("Structured output recovered fenced JSON for %s", model.__name__)
                return parsed
        parsed = model.model_validate(result)
        logger.debug("Structured output validated for %s", model.__name__)
        return parsed
    except ValidationError as exc:
        recovered = _recover_from_validation_error(exc, model)
        if recovered is not None:
            logger.debug(
                "Structured output recovered %s from fenced JSON in validation error",
                model.__name__,
            )
            return recovered
        logger.warning(
            "Structured output failed for %s (%s); falling back to manual JSON parse",
            model.__name__,
            type(exc).__name__,
        )
        response = (prompt | llm).invoke(variables)
        content = _message_content(response)
        logger.debug("Raw LLM response for %s: %s", model.__name__, content)
        return parse_model_response(content, model)
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
