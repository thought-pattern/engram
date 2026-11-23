"""Startup data an Engram reads before it serves.

Seed files, the conversation tables (properties, sets, maps, predicates and
substitutions) and the STATIC corpus loaded into a fresh Engram. Each function that
takes ``engram`` is bound on ``engram.core.Engram`` as the method of the same name.
"""

from json import JSONDecodeError as json_JSONDecodeError, load as json_load
from os import path as os_path

from engram.constants import CONVERSATION_DUPLICATE_POLICIES, DEFAULT_BOT_NAME, DEFAULT_CONVERSATION_DUPLICATE_POLICY
from engram.constants_artifacts import Tier
from engram.pattern import normalize_pattern


def normalize_seed_pair(pair, path: str) -> dict:
    """Validate one seed entry using the same rules as ``load_static_data``."""
    if not isinstance(pair, dict):
        raise ValueError(f"seed file {path} entries must be objects")
    text = pair.get("response", "")
    pattern = pair.get("pattern", "")
    that = pair.get("that", "")
    topic = pair.get("topic", "")
    template = pair.get("template", {})
    if not isinstance(text, str):
        raise ValueError(f"seed file {path} responses must be strings")
    if not all(isinstance(value, str) for value in (pattern, that, topic)):
        raise ValueError(f"seed file {path} pattern, that, and topic values must be strings")
    if not isinstance(template, dict):
        raise ValueError(f"seed file {path} templates must be objects")
    if not text.strip() and not template:
        raise ValueError(f"seed file {path} entries require a response or template")
    result = {"pattern": pattern, "response": text, "that": that, "topic": topic, "template": dict(template)}
    return result


def read_seed_file(path: str) -> list:
    """Read one ``{"pairs": [...]}`` seed file."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError("seed file paths must be non-empty strings")
    seed_path = path.strip()
    if not os_path.isfile(seed_path):
        raise ValueError(f"seed file {seed_path} is missing or is not a file")
    try:
        with open(seed_path, encoding="utf-8") as handle:
            data = json_load(handle)
    except json_JSONDecodeError as err:
        raise ValueError(f"seed file {seed_path} is not valid JSON") from err
    # A missing pairs key leaves the tuple default, which the list check rejects.
    pairs = data.get("pairs", ()) if isinstance(data, dict) else ()
    if not isinstance(pairs, list):
        raise ValueError(f"seed file {seed_path} requires a pairs array")
    result = [normalize_seed_pair(pair, seed_path) for pair in pairs]
    return result


def load_seed_files(paths, duplicate_policy: str = DEFAULT_CONVERSATION_DUPLICATE_POLICY) -> list:
    """
    Read seed files in listed order and return one concatenated pair list.

    ``paths`` is a list or tuple of seed file paths.
    """
    if isinstance(paths, str) or not isinstance(paths, (list, tuple)):
        raise ValueError("seed files must be a list of paths")
    if duplicate_policy not in CONVERSATION_DUPLICATE_POLICIES:
        raise ValueError("conversation duplicate_policy must be error, last, or first")
    pairs: list[dict] = []
    origins: dict[tuple[str, str, str], str] = {}
    indexes: dict[tuple[str, str, str], int] = {}
    for path in paths:
        for pair in read_seed_file(path):
            pair_pattern = pair.get("pattern", "")
            key = (
                normalize_pattern(pair_pattern),
                normalize_pattern(pair.get("that", "")),
                normalize_pattern(pair.get("topic", "")),
            )
            if key in origins:
                # origins and indexes are written together, so the key is in both.
                earlier_index = indexes.get(key, 0)
                if duplicate_policy == "error":
                    earlier = pairs[earlier_index].get("pattern", "")
                    earlier_path = origins.get(key, "")
                    raise ValueError(f"duplicate seed pattern {earlier!r} in {earlier_path} and {pair_pattern!r} in {path}")
                if duplicate_policy == "first":
                    continue
                pairs[earlier_index] = pair
                origins[key] = path
                continue
            indexes[key] = len(pairs)
            origins[key] = path
            pairs.append(pair)
    return pairs


def read_json_object(path: str, label: str) -> dict:
    """Read one JSON object file used by the conversation tables."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"{label} paths must be non-empty strings")
    table_path = path.strip()
    if not os_path.isfile(table_path):
        raise ValueError(f"{label} file {table_path} is missing or is not a file")
    try:
        with open(table_path, encoding="utf-8") as handle:
            data = json_load(handle)
    except json_JSONDecodeError as err:
        raise ValueError(f"{label} file {table_path} is not valid JSON") from err
    if not isinstance(data, dict):
        raise ValueError(f"{label} file {table_path} requires an object")
    return data


def read_string_map(path: str, label: str) -> dict[str, str]:
    """Read a flat JSON object of strings."""
    data = read_json_object(path, label)
    result = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"{label} file {path} keys must be non-empty strings")
        if not isinstance(value, str):
            raise ValueError(f"{label} file {path} values must be strings")
        result[key.strip()] = value
    return result


def read_set_file(path: str) -> dict[str, list[str]]:
    """Read a JSON object of set name to member words."""
    data = read_json_object(path, "set")
    result = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"set file {path} names must be non-empty strings")
        if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
            raise ValueError(f"set file {path} entries must be lists of non-empty strings")
        result[key.strip()] = [item.strip() for item in value]
    return result


def read_map_file(path: str) -> dict[str, dict[str, str]]:
    """Read a JSON object of map name to string tables."""
    data = read_json_object(path, "map")
    result = {}
    for key, value in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ValueError(f"map file {path} names must be non-empty strings")
        if not isinstance(value, dict):
            raise ValueError(f"map file {path} entries must be objects of strings")
        table = {}
        for inner_key, inner_value in value.items():
            if not isinstance(inner_key, str) or not inner_key.strip() or not isinstance(inner_value, str):
                raise ValueError(f"map file {path} entries must be objects of strings")
            table[inner_key.strip()] = inner_value
        result[key.strip()] = table
    return result


SUBSTITUTION_KEYS = {"contractions", "person", "person2", "gender", "custom"}


def read_substitution_file(path: str) -> dict[str, dict[str, str]]:
    """Read optional contraction, person, gender, and custom substitution tables.

    An unknown table name raises ``ValueError`` naming it and the file.
    """
    data = read_json_object(path, "substitution")
    unknown = sorted(str(key) for key in data if key not in SUBSTITUTION_KEYS)
    if unknown:
        raise ValueError(f"substitution file {path} has unknown table(s): {', '.join(unknown)}")
    result = {}
    for key, value in data.items():
        if not isinstance(value, dict):
            raise ValueError(f"substitution file {path} tables must be objects of strings")
        table = {}
        for inner_key, inner_value in value.items():
            if not isinstance(inner_key, str) or not inner_key.strip() or not isinstance(inner_value, str):
                raise ValueError(f"substitution file {path} tables must be objects of strings")
            table[inner_key.strip().lower()] = inner_value
        result[key] = table
    return result


def load_conversation_tables(engram, settings: dict) -> None:
    """Load properties, sets, maps, predicates, and substitutions before categories.

    A later file replaces the same set name, map name, or property. Substitutions
    merge over the built-in tables. ``bot_name`` wins
    over a properties file. ``settings`` is the validated ``conversation_config``
    section, whose file lists and optional paths are always present.
    """
    bot_properties = engram.pattern_matcher.bot_properties()
    properties_file = settings.get("properties_file", "")
    if properties_file:
        bot_properties.update(read_string_map(properties_file, "properties"))
    bot_properties["name"] = settings.get("bot_name", DEFAULT_BOT_NAME).strip()
    word_sets = engram.pattern_matcher.word_sets()
    for path in settings.get("set_files", []):
        for name, members in read_set_file(path).items():
            word_sets[name] = members
    engram.pattern_matcher.replace_bot_properties(bot_properties)
    engram.pattern_matcher.replace_sets(word_sets)
    for path in settings.get("map_files", []):
        for name, table in read_map_file(path).items():
            engram.maps[name] = table
    predicate_file = settings.get("predicate_file", "")
    if predicate_file:
        engram.default_predicates.update(read_string_map(predicate_file, "predicate"))
    substitution_file = settings.get("substitution_file", "")
    if substitution_file:
        for key, table in read_substitution_file(substitution_file).items():
            # Only SUBSTITUTION_KEYS tables reach here, and the built-in maps hold each of them.
            merged_table = dict(engram.substitution_maps.get(key, {}))
            merged_table.update(table)
            engram.substitution_maps[key] = merged_table


def load_corpus(engram, statements: list[str], tier: Tier = Tier.STATIC) -> int:
    """Load a corpus of statements.

    Args:
        statements: List of statement texts.
        tier: Tier for all statements.

    Returns:
        Number of statements loaded.
    """
    for text in statements:
        engram.store(text, tier=tier)
    count = len(statements)
    return count


def load_static_data(engram, pairs: list[dict]) -> int:
    """Load the provided structured STATIC corpus into a fresh Engram.

    Static data is startup input, not recovered state. Loading fails after
    any statement, session, accepted response, or mutation receipt exists,
    so this operation cannot synchronize a running process or carry dynamic
    memory into a new one.
    """
    if not isinstance(pairs, list):
        raise ValueError("static data must be a list of objects")

    normalized_pairs = []
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("each static data entry must be an object")
        text = pair.get("response", "")
        pattern = pair.get("pattern", "")
        that = pair.get("that", "")
        topic = pair.get("topic", "")
        template = pair.get("template", {})
        if not isinstance(text, str):
            raise ValueError("static responses must be strings")
        if not all(isinstance(value, str) for value in (pattern, that, topic)):
            raise ValueError("static pattern, that, and topic values must be strings")
        if not isinstance(template, dict):
            raise ValueError("static templates must be objects")
        if not text.strip() and not template:
            raise ValueError("each static data entry requires a response or template")
        normalized_pairs.append({"text": text, "pattern": pattern, "that": that, "topic": topic, "template": dict(template)})

    with engram.mutation_lock, engram.statement_lock, engram.session_lock:
        artifacts = engram.response_repository.trusted_artifacts()
        has_process_state = (
            bool(engram.statements)
            or bool(engram.sessions)
            or bool(artifacts)
            or engram.mutation_receipts.next_sequence != 1
            or engram.query_count != 0
            or engram.hit_count != 0
            or engram.eviction_count != 0
        )
        if has_process_state:
            raise ValueError("static data can only be loaded into a fresh Engram")
        for pair in normalized_pairs:
            engram.store(
                pair.get("text", ""),
                tier=Tier.STATIC,
                pattern=pair.get("pattern", ""),
                that=pair.get("that", ""),
                topic=pair.get("topic", ""),
                template=pair.get("template", {}),
            )
    result = len(normalized_pairs)
    return result
