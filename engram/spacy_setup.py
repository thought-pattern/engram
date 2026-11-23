"""Centralized spaCy model management for ENGRAM.

spaCy provides a dependency parser, which NLTK lacks, enabling relational fact
extraction (subject-verb-object triples) beyond the copula-only extractor. The
English model ``en_core_web_sm`` is a pip-installed package, so it is present at
setup time and never fetched during normal runtime.

``SPACY_PIPELINES`` owns the loaded pipeline variants of that model for the
process; callers ask it for the variant they need.

Install once after dependencies:

    python -m spacy download en_core_web_sm
"""

from threading import Lock

from spacy import load as spacy_load
from spacy.language import Language

from engram.constants_language import MODEL_NAME

# Distinct disable-sets kept loaded at once. Callers use the full pipeline and
# the parser/NER-free phrasing variant, so the bound is never reached in use.
PIPELINE_CACHE_CAPACITY = 4


class SpacyPipelines:
    """Own the loaded pipelines of one pre-provisioned spaCy model.

    Each distinct ``disable`` set is loaded once, under the lock, and reused.
    The model is a hard runtime requirement: a model that cannot be loaded raises
    ``RuntimeError``. At most ``PIPELINE_CACHE_CAPACITY`` variants stay loaded;
    the least recently used one is released first.
    """

    def __init__(self, model_name: str) -> None:
        self.model_name = model_name
        self.lock = Lock()
        self.pipelines: dict[tuple, Language] = {}

    def pipeline(self, disable=()) -> Language:
        """Return the pipeline with the ``disable`` components switched off.

        Args:
            disable: Pipeline component names to disable for speed. Defaults to
                the full pipeline (tagger, parser, NER, lemmatizer). Pass e.g.
                ("ner",) to skip components not needed.

        Returns:
            A loaded spaCy Language object.

        Raises:
            RuntimeError: the pre-provisioned model is not installed.
        """
        key = tuple(disable)
        with self.lock:
            # A cached pipeline is re-inserted so dictionary order stays least recently used first.
            nlp = self.pipelines.pop(key) if key in self.pipelines else self.load(key)
            self.pipelines[key] = nlp
            while len(self.pipelines) > PIPELINE_CACHE_CAPACITY:
                del self.pipelines[next(iter(self.pipelines))]
        return nlp

    def load(self, disable: tuple) -> Language:
        """Load the model with ``disable`` components off; raise ``RuntimeError`` when it is not installed."""
        try:
            nlp = spacy_load(self.model_name, disable=list(disable))
        except OSError as err:
            raise RuntimeError(
                f"spaCy model {self.model_name} is not installed; run: python -m spacy download {self.model_name}"
            ) from err
        return nlp


SPACY_PIPELINES = SpacyPipelines(MODEL_NAME)
