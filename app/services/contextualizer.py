from __future__ import annotations

import re
from dataclasses import dataclass

from langchain_core.language_models import BaseChatModel
from langchain_core.prompts import ChatPromptTemplate
from pydantic import BaseModel, Field

from app.db.models import Message
from app.helpers.llm_parsing import invoke_structured

_CUE_PHRASES = (
    "what about",
    "how about",
    "same but",
    "same as",
    "but with",
    "but for",
    "except",
    "without the",
    "instead of",
    "lighter",
    "heavier",
    "shorter",
    "longer",
    "simpler",
    "cheaper",
    "more",
    "less",
    "another",
    "other",
    "similar",
    "quicker",
    "easier",
    "harder",
    "faster",
    "slower",
    "bigger",
    "smaller",
)
_CUE_WORDS = re.compile(
    r"\b(?:those ones|it|that|those|them|also|they)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class QueryPlan:
    standalone_query: str
    topic_changed: bool


class StandaloneQuery(BaseModel):
    standalone_query: str = Field(min_length=1)


class TopicSwitchOutput(BaseModel):
    topic_changed: bool
    standalone_query: str = Field(min_length=1)


class ConversationSummary(BaseModel):
    summary: str = Field(min_length=1)


CONTEXTUALIZE_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You rewrite board-game recommendation follow-ups into one standalone search prompt.\n"
            "Rules:\n"
            "1. Resolve pronouns and short follow-ups using the conversation summary and recent messages.\n"
            "2. Preserve earlier constraints (players, time, complexity, similar-to, categories) unless the "
            "follow-up clearly replaces them.\n"
            "3. Do not invent game names or constraints absent from the conversation.\n"
            "4. Return only the standalone_query field.",
        ),
        (
            "human",
            "Summary:\n{summary}\n\nRecent messages:\n{recent_messages}\n\nCurrent user message:\n{query}",
        ),
    ]
)

PLAN_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "You plan a board-game recommendation follow-up.\n"
            "Rules:\n"
            "1. Set topic_changed true when the current message is a new recommendation request that must "
            "not keep prior players, time, complexity/weight, similar-to, or categories.\n"
            "2. Otherwise set topic_changed false and rewrite into one standalone search prompt. Preserve "
            "earlier constraints unless the follow-up clearly replaces them.\n"
            "3. Do not invent game names or filters absent from the conversation.\n"
            "4. Return JSON only with topic_changed and standalone_query.",
        ),
        (
            "human",
            "Summary:\n{summary}\n\nRecent messages:\n{recent_messages}\n\nCurrent user message:\n{query}",
        ),
    ]
)

SUMMARY_PROMPT = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            "Update the running summary of a board-game recommendation chat. "
            "Keep stable preferences and constraints. Be concise.",
        ),
        (
            "human",
            "Prior summary:\n{prior_summary}\n\nDropped turn:\nUser: {user_content}\nAssistant: {assistant_content}",
        ),
    ]
)


def has_followup_cue(query: str) -> bool:
    lowered = query.lower()
    if any(phrase in lowered for phrase in _CUE_PHRASES):
        return True
    return _CUE_WORDS.search(query) is not None


def _format_recent(messages: list[Message]) -> str:
    lines: list[str] = []
    for m in messages:
        lines.append(f"{m.role}: {m.content}")
    return "\n".join(lines) if lines else "(none)"


def contextualize_query(
    llm: BaseChatModel,
    *,
    query: str,
    summary: str | None,
    recent_messages: list[Message],
) -> QueryPlan:
    if not recent_messages:
        return QueryPlan(standalone_query=query, topic_changed=False)
    variables = {
        "summary": summary or "(none)",
        "recent_messages": _format_recent(recent_messages),
        "query": query,
    }
    if has_followup_cue(query):
        result = invoke_structured(llm, CONTEXTUALIZE_PROMPT, StandaloneQuery, variables)
        return QueryPlan(standalone_query=result.standalone_query.strip(), topic_changed=False)
    result = invoke_structured(llm, PLAN_PROMPT, TopicSwitchOutput, variables)
    if result.topic_changed:
        return QueryPlan(standalone_query=query, topic_changed=True)
    return QueryPlan(standalone_query=result.standalone_query.strip(), topic_changed=False)


def summarize_dropped_turn(
    llm: BaseChatModel,
    *,
    prior_summary: str | None,
    user_content: str,
    assistant_content: str,
) -> str:
    result = invoke_structured(
        llm,
        SUMMARY_PROMPT,
        ConversationSummary,
        {
            "prior_summary": prior_summary or "(none)",
            "user_content": user_content,
            "assistant_content": assistant_content,
        },
    )
    return result.summary.strip()
