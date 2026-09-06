from unittest.mock import MagicMock

from app.services.contextualizer import (
    QueryPlan,
    contextualize_query,
    has_followup_cue,
    summarize_dropped_turn,
)
from app.db.models import Message


def test_has_followup_cue_matches_and_rejects() -> None:
    assert has_followup_cue("also 2-player")
    assert has_followup_cue("something lighter")
    assert has_followup_cue("instead of Catan")
    assert has_followup_cue("more players")
    assert has_followup_cue("what about 2p")
    assert not has_followup_cue("this weekend war games")
    assert not has_followup_cue("war games instead")
    assert not has_followup_cue("2-player war games")


def test_contextualize_skips_llm_without_history() -> None:
    llm = MagicMock()
    result = contextualize_query(llm, query="games for 4", summary=None, recent_messages=[])
    assert result == QueryPlan(standalone_query="games for 4", topic_changed=False)
    llm.assert_not_called()


def test_contextualize_cue_uses_rewrite_only(monkeypatch) -> None:
    from app.services import contextualizer as mod

    calls: list[type] = []

    def fake_invoke(llm, prompt, model, variables):
        calls.append(model)
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
    assert result == QueryPlan(
        standalone_query="light games for 4 players", topic_changed=False
    )
    assert calls == [mod.StandaloneQuery]


def test_contextualize_no_cue_switch_discards_rewrite(monkeypatch) -> None:
    from app.services import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(topic_changed=True, standalone_query="smuggle party games for 8")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="party games for 8"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="2-player war games",
        summary=None,
        recent_messages=recent,
    )
    assert result == QueryPlan(standalone_query="2-player war games", topic_changed=True)


def test_contextualize_no_cue_follow_up_uses_model_rewrite(monkeypatch) -> None:
    from app.services import contextualizer as mod

    def fake_invoke(llm, prompt, model, variables):
        return model(topic_changed=False, standalone_query="cooperative games for 4")

    monkeypatch.setattr(mod, "invoke_structured", fake_invoke)
    recent = [
        Message(role="user", content="games for 4 players"),
        Message(role="assistant", content="Here are some options."),
    ]
    result = contextualize_query(
        MagicMock(),
        query="cooperative games",
        summary=None,
        recent_messages=recent,
    )
    assert result == QueryPlan(
        standalone_query="cooperative games for 4", topic_changed=False
    )


def test_summarize_dropped_turn(monkeypatch) -> None:
    from app.services import contextualizer as mod

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
