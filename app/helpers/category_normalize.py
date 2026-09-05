from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Category
from app.api.models import ExtractedFilters


def _slug_key(name: str) -> str:
    return name.strip().lower().replace(" ", "_")


def _build_lookup(session: Session) -> dict[str, str]:
    names = session.scalars(select(Category.name)).all()
    lookup: dict[str, str] = {}
    for name in names:
        lookup[_slug_key(name)] = name
        lookup[name.strip().lower()] = name
    return lookup


def normalize_categories(
    session: Session, categories: list[str] | None
) -> tuple[list[str], list[str]]:
    if not categories:
        return [], []

    lookup = _build_lookup(session)
    resolved: list[str] = []
    leftovers: list[str] = []
    seen: set[str] = set()

    for label in categories:
        key = _slug_key(label)
        match = lookup.get(key) or lookup.get(label.strip().lower())
        if match is None:
            leftovers.append(label)
            continue
        if match not in seen:
            seen.add(match)
            resolved.append(match)

    return resolved, leftovers


def apply_category_normalization(
    session: Session, filters: ExtractedFilters
) -> ExtractedFilters:
    resolved, leftovers = normalize_categories(session, filters.categories)
    keywords = list(filters.keywords or [])
    for item in leftovers:
        if item not in keywords:
            keywords.append(item)

    data = filters.model_dump()
    data["categories"] = resolved or None
    data["keywords"] = keywords or None
    return ExtractedFilters.model_validate(data)
