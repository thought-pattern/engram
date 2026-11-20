"""Output polish for ENGRAM responses.

Template substitution splices lowercase wildcard captures into authored text,
so a response can read "nice to know you're tired i have been working". This
module applies the mechanical casing repairs a reader expects: each sentence
starts with a capital letter, and the pronoun I (and its contractions) is
always capitalized. It deliberately does no grammar correction and inserts no
punctuation -- templates author their own punctuation, and anything beyond
casing risks rewriting content.
"""

from re import compile as re_compile

from engram.constants_language import STANDALONE_I_FORMS
from engram.substitutions import apply_substitutions, split_sentences

CODE_SPAN_PATTERN = re_compile(r"(```[\s\S]*?```|~~~[\s\S]*?~~~|`[^`\n]+`)")
PARAGRAPH_BREAK_PATTERN = re_compile(r"(\n[ \t]*\n+)")
CODE_TOKEN_CHARACTERS = set("()[]{}=_/:0123456789@#$<>+*%|\\^~`")


def should_capitalize_token(token: str) -> bool:
    """Return whether a sentence-initial token is lowercase prose rather than code or a cased identifier."""
    if not token or not token[0].islower():
        result = False
        return result
    core = token.rstrip(".!?,;:")
    if not core or (token.endswith(":") and len(core) == 1):
        result = False
        return result
    if any(char.isupper() or char in CODE_TOKEN_CHARACTERS for char in core) or "." in core:
        result = False
        return result
    result = True
    return result


def polish_sentence(sentence: str) -> str:
    """Capitalize a sentence's opening prose word when appropriate."""
    tokens = sentence.split(maxsplit=1)
    if not tokens or not should_capitalize_token(tokens[0]):
        return sentence
    polished = sentence[0].upper() + sentence[1:]
    return polished


def polish_paragraph(paragraph: str) -> str:
    """Polish standalone I forms and sentence starts within one prose paragraph in place."""
    if not paragraph or not paragraph.strip():
        return paragraph
    substituted = apply_substitutions(paragraph, STANDALONE_I_FORMS)
    sentences = split_sentences(substituted)
    if not sentences:
        return substituted
    pieces: list[str] = []
    cursor = 0
    for sentence in sentences:
        position = substituted.find(sentence, cursor)
        if position < 0:
            continue
        pieces.append(substituted[cursor:position])
        pieces.append(polish_sentence(sentence))
        cursor = position + len(sentence)
    pieces.append(substituted[cursor:])
    polished = "".join(pieces)
    return polished


def polish_response(text: str) -> str:
    """Repair casing in a response: sentence starts and the pronoun I.

    Args:
        text: Response text (may contain several sentences, paragraphs, or code).

    Returns:
        The text with prose sentence-initial letters and standalone I capitalized,
        preserving line breaks, paragraph breaks, indentation, code spans, and identifiers.
    """
    if not text or not text.strip():
        return text

    segments = CODE_SPAN_PATTERN.split(text)
    output: list[str] = []
    for segment_index, segment in enumerate(segments):
        if segment_index % 2 == 1:
            output.append(segment)
            continue
        paragraphs = PARAGRAPH_BREAK_PATTERN.split(segment)
        for paragraph_index, part in enumerate(paragraphs):
            if paragraph_index % 2 == 1:
                output.append(part)
            else:
                output.append(polish_paragraph(part))
    polished = "".join(output)
    return polished
