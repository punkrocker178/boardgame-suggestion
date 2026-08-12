from app.category_normalize import normalize_categories
from app.db.models import Category
from app.models import ExtractedFilters


def test_normalize_case_and_slug(db_session) -> None:
    db_session.add_all(
        [
            Category(id=1, name="Strategy"),
            Category(id=2, name="Card Game"),
        ]
    )
    db_session.commit()

    filters = ExtractedFilters(categories=["strategy", "card_game", "cozy_vibes"])
    resolved, leftovers = normalize_categories(db_session, filters.categories)

    assert resolved == ["Strategy", "Card Game"]
    assert leftovers == ["cozy_vibes"]


def test_normalize_empty_and_none(db_session) -> None:
    assert normalize_categories(db_session, None) == ([], [])
    assert normalize_categories(db_session, []) == ([], [])


def test_apply_category_normalization_merges_keywords(db_session) -> None:
    from app.category_normalize import apply_category_normalization

    db_session.add(Category(id=1, name="Party"))
    db_session.commit()

    filters = ExtractedFilters(
        categories=["party", "unknown_tag"],
        keywords=["funny"],
    )
    out = apply_category_normalization(db_session, filters)
    assert out.categories == ["Party"]
    assert out.keywords == ["funny", "unknown_tag"]


def test_apply_category_normalization_clears_when_none_resolve(db_session) -> None:
    from app.category_normalize import apply_category_normalization

    db_session.add(Category(id=1, name="Strategy"))
    db_session.commit()

    filters = ExtractedFilters(categories=["not_a_real_category"])
    out = apply_category_normalization(db_session, filters)
    assert out.categories is None
    assert out.keywords == ["not_a_real_category"]
