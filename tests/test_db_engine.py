from app.db.engine import _normalize_database_url


def test_normalize_database_url_uses_psycopg_driver() -> None:
    assert (
        _normalize_database_url("postgresql://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
    assert (
        _normalize_database_url("postgres://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
    assert (
        _normalize_database_url("postgresql+psycopg://user:pass@localhost/db")
        == "postgresql+psycopg://user:pass@localhost/db"
    )
