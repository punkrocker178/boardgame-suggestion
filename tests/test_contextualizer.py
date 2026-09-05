from unittest.mock import MagicMock

from app.contextualizer import contextualize_query, summarize_dropped_turn
from app.db.models import Message


def test_contextualize_skips_llm_without_history() -> None:
    llm = MagicMock()
    result = contextualize_query(llm, query="games for 4", summary=None, recent_messages=[])
    assert result == "games for 4"
    llm.assert_not_called()


def test_contextualize_calls_llm_with_history(monkeypatch) -> None:
    from app import contextualizer as mod

    calls: list[dict] = []

    def fake_invoke(llm, prompt, model, variables):
        calls.append(variables)
        return model(standalone_query="light games for 4 players")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="games for 4 players"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="something lighter",
        summary=None,
        recent_messages=recent,
    )
    assert result == "light games for 4 players"
    assert "something lighter" in calls[0]["query"]
    assert "games for 4 players" in calls[0]["recent_messages"]


def test_summarize_dropped_turn(monkeypatch) -> None:
    from app import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(summary="User wants 4-player games.")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    out = summarize_dropped_turn(
        MagicMock(),
        prior_summary=None,
        user_content="games for 4",
        assistant_content="Suggested Catan.",
    )
    assert out == "User wants 4-player games."
