import pytest

from app.text_extractor import sentence_count


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("one", 1),
        ("a. b. c", 3),
        ("a. b. c. d", 4),
        ("Really? Yes!", 2),
        ("Hi.  ", 1),
        ("", 0),
    ],
)
def test_sentence_count(query: str, expected: int) -> None:
    assert sentence_count(query) == expected
