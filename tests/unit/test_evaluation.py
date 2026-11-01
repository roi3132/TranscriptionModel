"""Check accuracy scoring independently of model downloads and hardware."""

import pytest

from scripts.smoke_media import reference_words, word_errors


@pytest.mark.parametrize(
    ("reference", "hypothesis", "expected"),
    [
        ("Hello, WORLD!", "hello world", (0, 2)),
        ("one two three", "one four three", (1, 3)),
        ("one two three", "one three", (1, 3)),
        ("one two", "one extra two", (1, 2)),
        ("one", "one two three", (2, 1)),
        ("hello world", "", (2, 2)),
        ("שָׁלוֹם עוֹלָם", "שלום עולם", (0, 2)),
    ],
)
def test_word_errors(reference: str, hypothesis: str, expected: tuple[int, int]) -> None:
    assert word_errors(reference, hypothesis) == expected


def test_empty_reference_is_not_a_perfect_score() -> None:
    with pytest.raises(ValueError, match="at least one word"):
        word_errors("...", "hello")


def test_normalization_keeps_hebrew_words_and_numbers() -> None:
    assert reference_words("שלום, עולם! 42") == ["שלום", "עולם", "42"]
