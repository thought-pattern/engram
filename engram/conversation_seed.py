"""Packaged conversational STATIC data for host-owned Engram processes."""

from importlib.resources import files
from json import JSONDecodeError as json_JSONDecodeError, loads as json_loads
from pathlib import Path

DEFAULT_CONVERSATION_SEED_RESOURCE = "data/seed.json"


def conversation_seed_pairs_from_text(text: str) -> list[dict]:
    """Decode one closed conversation-seed document into copied pair records."""
    if not isinstance(text, str):
        raise ValueError("conversation seed must be JSON text")
    try:
        data = json_loads(text)
    except json_JSONDecodeError as error:
        raise ValueError(f"conversation seed is invalid JSON: {error.msg}") from error
    if not isinstance(data, dict) or set(data) != {"pairs"}:
        raise ValueError("conversation seed must be an object containing only pairs")
    raw_pairs = data.get("pairs")
    if not isinstance(raw_pairs, list):
        raise ValueError("conversation seed pairs must be a list")
    if not all(isinstance(pair, dict) for pair in raw_pairs):
        raise ValueError("each conversation seed pair must be an object")
    result = [dict(pair) for pair in raw_pairs]
    return result


def load_bundled_conversation_pairs() -> list[dict]:
    """Load the package-owned default conversational corpus."""
    resource = files("engram").joinpath(DEFAULT_CONVERSATION_SEED_RESOURCE)
    result = conversation_seed_pairs_from_text(resource.read_text(encoding="utf-8"))
    return result


def load_conversation_pairs(path: str = "") -> list[dict]:
    """Load a host-selected corpus or the bundled default when path is empty."""
    if not isinstance(path, str):
        raise ValueError("conversation seed path must be a string")
    if not path:
        return load_bundled_conversation_pairs()
    selected = Path(path)
    try:
        text = selected.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError(f"could not read conversation seed: {selected}") from error
    result = conversation_seed_pairs_from_text(text)
    return result


def require_conversation_catch_all(pairs: list[dict]) -> None:
    """Require the deterministic wildcard needed for non-empty MCP replies."""
    if not isinstance(pairs, list):
        raise ValueError("conversation seed pairs must be a list")
    for pair in pairs:
        pattern = pair.get("pattern", "") if isinstance(pair, dict) else ""
        if isinstance(pattern, str) and pattern.strip() == "*":
            return
    raise ValueError("conversational MCP static data requires a '*' catch-all pattern")
