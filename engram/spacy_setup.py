"""Centralized spaCy model management for ENGRAM.

spaCy provides a dependency parser, which NLTK lacks, enabling relational fact
extraction (subject-verb-object triples) beyond the copula-only extractor. The
English model ``en_core_web_sm`` is a pip-installed package, so it is present at
setup time and never fetched during normal runtime.

Install once after dependencies:

    python -m spacy download en_core_web_sm
"""

from functools import lru_cache

MODEL_NAME = "en_core_web_sm"


def _load(model_name: str, disable):
    """Load a spaCy model, downloading once if absent. Returns () on failure."""
    import spacy

    try:
        return spacy.load(model_name, disable=list(disable))
    except OSError:
        from spacy.cli import download

        download(model_name)
    try:
        return spacy.load(model_name, disable=list(disable))
    except OSError:
        return ()


@lru_cache(maxsize=4)
def get_nlp(disable=()):
    """Load and cache the spaCy English pipeline (tagger, parser, NER, lemmatizer).

    Args:
        disable: Pipeline component names to disable for speed. Defaults to the
            full pipeline. Pass e.g. ("ner",) to skip components not needed.

    Returns:
        A loaded spaCy Language object, or falsy () if the model cannot be
        loaded (so callers degrade gracefully rather than crash).
    """
    return _load(MODEL_NAME, disable)
