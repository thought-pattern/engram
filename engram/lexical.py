"""Dependency-free lexical term selection shared by retrieval pipelines."""

from collections.abc import Sequence


def select_lexical_terms(
    tokens: Sequence[str],
    stopwords: set[str],
    *,
    allow_technical: bool = False,
    max_terms: int = 0,
) -> list[str]:
    """Lowercase, filter, and deduplicate tokens while preserving order.

    Args:
        tokens: Already-tokenized input strings.
        stopwords: Lowercase terms to exclude.
        allow_technical: Retain tokens containing technical punctuation when
            they also contain at least one alphanumeric character.
        max_terms: Positive output bound; zero means the input length.

    Returns:
        Ordered unique lexical terms.
    """
    limit = max_terms if max_terms > 0 else len(tokens)
    seen: set[str] = set()
    terms = []
    for token in tokens:
        word = token.lower()
        is_lexical = any(character.isalnum() for character in word) if allow_technical else word.isalnum()
        if not is_lexical or word in stopwords or word in seen:
            continue
        seen.add(word)
        terms.append(word)
        if len(terms) == limit:
            break
    return terms
