"""Compare the copula (NLTK) and dependency-parse (spaCy) fact extractors.

The NLTK extractor (engram.nlp.extract_fact) only recognizes copula sentences
("X is/are/was/were Y"). The spaCy extractor (engram.facts_spacy.extract_facts)
adds action-verb SVO triples and prepositional relations. This script runs both
over a corpus and prints what each captures, so the added coverage is visible.

Usage:
    python eval/compare_facts.py
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from engram.facts_spacy import extract_facts
from engram.nlp import extract_fact

CORPUS = [
    "The sky is blue.",
    "Cats are mammals.",
    "Paris is the capital of France.",
    "Paris is in France.",
    "Einstein developed the theory of relativity.",
    "The Eiffel Tower has 1665 steps.",
    "Dolphins use echolocation.",
    "The book belongs to Mary.",
    "Shakespeare wrote Hamlet.",
    "Water boils at 100 degrees.",
    "The company hired three engineers.",
    "Mount Everest stands in Nepal.",
]


def _triple(fact: dict) -> str:
    """Render a fact dict as a compact triple string."""
    result = f"({fact['subject']}, {fact['predicate']}, {fact['obj']})"
    return result


def main() -> int:
    """Run both extractors over the corpus and print a comparison."""
    copula_hits = 0
    spacy_hits = 0

    print("Fact extraction: copula (NLTK) vs dependency parse (spaCy)")
    print("=" * 72)
    for sentence in CORPUS:
        copula = extract_fact(sentence)
        spacy_facts = extract_facts(sentence)
        copula_hits += 1 if copula else 0
        spacy_hits += len(spacy_facts)

        copula_str = _triple(copula) if copula else "-"
        spacy_str = "; ".join(_triple(f) for f in spacy_facts) if spacy_facts else "-"
        print(f"\n{sentence}")
        print(f"  copula: {copula_str}")
        print(f"  spaCy : {spacy_str}")

    print("\n" + "=" * 72)
    print(f"Sentences:        {len(CORPUS)}")
    print(f"Copula extracted: {copula_hits}")
    print(f"spaCy extracted:  {spacy_hits}")
    result = 0
    return result


if __name__ == "__main__":
    sys.exit(main())
