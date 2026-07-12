"""Output polish for ENGRAM responses.

Template substitution splices lowercase wildcard captures into authored text,
so a response can read "nice to know you're tired i have been working". This
module applies the mechanical casing repairs a reader expects: each sentence
starts with a capital letter, and the pronoun I (and its contractions) is
always capitalized. It deliberately does no grammar correction and inserts no
punctuation -- templates author their own punctuation, and anything beyond
casing risks rewriting content.
"""

from engram.constants import STANDALONE_I_FORMS
from engram.substitutions import apply_substitutions, split_sentences


def polish_response(text: str) -> str:
    """Repair casing in a response: sentence starts and the pronoun I.

    Args:
        text: Response text (may contain several sentences).

    Returns:
        The text with sentence-initial letters and standalone I capitalized.
    """
    if not text or not text.strip():
        return text

    result = apply_substitutions(text, STANDALONE_I_FORMS)

    sentences = split_sentences(result)
    if not sentences:
        return result

    capitalized = []
    for sentence in sentences:
        if sentence[0].isalpha():
            sentence = sentence[0].upper() + sentence[1:]
        capitalized.append(sentence)

    polished = " ".join(capitalized)
    return polished
