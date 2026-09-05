from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.api.models import RecommendRequest


def test_recommend_request_requires_conversation_id() -> None:
    with pytest.raises(ValidationError):
        RecommendRequest(query="hello")


def test_recommend_request_accepts_conversation_id() -> None:
    cid = uuid4()
    req = RecommendRequest(query="hello", conversation_id=cid)
    assert req.conversation_id == cid
