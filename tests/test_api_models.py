from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.models import RECOMMEND_QUERY_MAX_CHARS, RecommendRequest


def test_recommend_request_requires_conversation_id() -> None:
    with pytest.raises(ValidationError):
        RecommendRequest(query="hello")


def test_recommend_request_accepts_conversation_id() -> None:
    cid = uuid4()
    req = RecommendRequest(query="hello", conversation_id=cid)
    assert req.conversation_id == cid


def test_recommend_request_accepts_max_query_length() -> None:
    cid = uuid4()
    req = RecommendRequest(
        query="a" * RECOMMEND_QUERY_MAX_CHARS,
        conversation_id=cid,
    )
    assert len(req.query) == RECOMMEND_QUERY_MAX_CHARS


def test_recommend_request_rejects_overlong_query() -> None:
    with pytest.raises(ValidationError):
        RecommendRequest(
            query="a" * (RECOMMEND_QUERY_MAX_CHARS + 1),
            conversation_id=uuid4(),
        )
