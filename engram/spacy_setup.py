"""Centralized spaCy model management for ENGRAM.

spaCy provides a dependency parser, which NLTK lacks, enabling relational fact
extraction (subject-verb-object triples) beyond the copula-only extractor. The
English model ``en_core_web_sm`` is a pip-installed package, so it is present at
setup time and never fetched during normal runtime.

Install once after dependencies:

    python -m spacy download en_core_web_sm
"""

from functools import lru_cache

import spacy

from engram.constants import MODEL_NAME


def _load(model_name: str, disable):
    """Load a pre-provisioned spaCy model. Returns () when it is absent."""
    try:
        nlp = spacy.load(model_name, disable=list(disable))
        return nlp
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
    nlp = _load(MODEL_NAME, disable)
    return nlp
