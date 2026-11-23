"""Read-only graph recall for an Engram.

Cypher reads, the startup-loaded query embedding model, vector and structured
Proposition projections, canonical entity and Predicate matches, one-hop relation reads
and phrased fact lookup. A failed read withholds recall instead of raising into the
response path. Each function that takes ``engram`` is bound on ``engram.core.Engram`` as
the method of the same name.
"""

from logging import getLogger as logging_getLogger
from pathlib import Path
from time import monotonic_ns as time_monotonic_ns

from numpy import ndarray
from sentence_transformers import SentenceTransformer

from engram.constants import MAX_GRAPH_VECTOR_LIMIT
from engram.constants_graph import (
    GRAPH_ENTITY_FACTS_QUERY,
    GRAPH_KEYWORD_FACTS_QUERY,
    MAX_PROPOSITION_PROJECTION_ROWS,
    MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS,
    UNCONSTRAINED_ASSERTION_BASIS,
    PropositionProjectionQuery,
)
from engram.constants_relation import MAX_RELATION_CANDIDATES, MAX_RELATION_PLAN_ROWS
from engram.errors import failure_origin
from engram.graph import (
    canonical_entity_match_from_graph_row,
    canonical_predicate_match_from_graph_row,
    is_write_cypher,
    projection_timestamp,
    proposition_projection_to_dict,
    validate_proposition_projection,
    validate_relation_proposition_projection,
)
from engram.nlp import extract_entities
from engram.phrasing import phrase_facts
from engram.resources import estimate_working_bytes, require_working_memory, run_cooperative_check
from engram.telemetry import record_graph_recall
from engram.text import extract_keywords, normalize
from engram.validation import format_utc_timestamp

logger = logging_getLogger("engram.core")


def graph_records_to_facts(records: list) -> list[tuple[str, str, str]]:
    """Turn canonical graph rows into complete fact tuples."""
    facts = []
    seen: set[tuple[str, str, str]] = set()
    for record in records:
        subject = record.get("subject", "")
        predicate = record.get("predicate", "")
        obj = record.get("object", "")
        fact = (subject, predicate, obj)
        identity = tuple(value.casefold() for value in fact)
        if subject and predicate and obj and identity not in seen:
            seen.add(identity)
            facts.append(fact)
    result = facts
    return result


def format_graph_facts(facts: list[tuple[str, str, str]]) -> str:
    """Phrase graph fact tuples as natural-language sentences."""
    result = phrase_facts(facts)
    return result


def failed_graph_read(operation: str, error: BaseException, empty):
    """Log a database failure in full at debug level and return the same empty result as no rows.

    A failed read only withholds graph recall from the turn; the caller sees an ordinary empty
    result, never the failure text.
    """
    logger.debug("Graph read failed (%s)", operation, exc_info=error)
    result = empty
    return result


def graph_query(engram, cypher: str, params=(), *, raise_on_failure: bool = False) -> list:
    """Execute a read-only Cypher query against the knowledge graph.

    Args:
        cypher: Cypher query string.
        params: Optional query parameters.

    Returns:
        List of row dicts. Empty list when the graph is not configured,
        unreachable, or the query fails -- recall degrades gracefully rather
        than raising into the template/response path.
    """
    if is_write_cypher(cypher):
        raise ValueError("ENGRAM graph access is read-only")

    if not engram.graph_client:
        result = []
        return result
    execute = engram.graph_operation("execute")
    # A failed read raises RuntimeError; a query or parameters the connection refuses (such as
    # a mutating clause or a non-mapping parameter set) raise ValueError. Both withhold recall.
    try:
        records = execute(cypher, params)
        if not isinstance(records, list):
            raise RuntimeError("graph read capability returned an invalid collection")
        return records
    except (RuntimeError, ValueError) as err:
        if raise_on_failure:
            raise
        result = failed_graph_read("graph_query", err, [])
        return result


def graph_read_fn(engram, cypher: str, params=()) -> list:
    """Read-only graph callback for template operations.

    Both this callback and the underlying connection reject mutating
    Cypher. The duplicated boundary keeps custom graph clients read-only.
    """
    result = engram.graph_query(cypher, params)
    return result


def load_graph_embedding_model(engram) -> None:
    """Load the configured local embedding model during initialization."""
    graph_config = engram.config.get("graph", {})
    model_name = str(graph_config.get("vector_model", "")).strip()
    model_path = str(graph_config.get("vector_model_path", "")).strip()
    dimension = int(graph_config.get("vector_dimension", 0))
    if not model_name or dimension < 1:
        raise ValueError("vector recall model and dimension must be configured")
    model_source = model_path or model_name
    if model_path and not Path(model_path).is_dir():
        raise FileNotFoundError(f"vector model path does not exist: {model_path}")
    engram.graph_embedding_model = SentenceTransformer(model_source, device="cpu", local_files_only=True)


def encode_graph_query(engram, text: str) -> list[float]:
    """Encode one graph-recall query with the startup-loaded local model."""
    graph_config = engram.config.get("graph", {})
    dimension = int(graph_config.get("vector_dimension", 0))
    if not engram.graph_embedding_model:
        raise RuntimeError("vector recall model was not initialized")
    encode = getattr(engram.graph_embedding_model, "encode", False)
    if not callable(encode):
        raise RuntimeError("vector recall model has no callable encode capability")
    encoded = encode([text], normalize_embeddings=True, show_progress_bar=False, convert_to_numpy=True)
    if not isinstance(encoded, ndarray) or encoded.ndim != 2 or len(encoded) != 1:
        raise RuntimeError("vector recall encoder must return one NumPy row")
    vector = encoded[0].tolist()
    if len(vector) != dimension:
        raise ValueError(f"query embedding dimension {len(vector)} does not match configured graph dimension {dimension}")
    return vector


def graph_vector_propositions(engram, text: str, *, limit: int = 0, evaluation_time: str = "") -> list:
    """Return active semantic Proposition hits for ``text``.

    The method fails soft because vector recall augments the deterministic
    keyword path; a model, index, or graph outage must not make Engram
    unavailable.
    """
    graph_config = engram.config.get("graph", {})
    if not engram.graph_client or not graph_config.get("enabled", False) or not graph_config.get("vector_enabled", False):
        result = []
        return result
    search = engram.graph_operation("vector_search_propositions")
    row_limit = max(1, min(MAX_GRAPH_VECTOR_LIMIT, limit)) if limit else graph_config.get("vector_limit", 0)
    try:
        embedding = engram.encode_graph_query(text)
        rows = search(
            embedding,
            index_name=graph_config.get("vector_index_name", ""),
            limit=row_limit,
            min_similarity=float(graph_config.get("vector_min_similarity", 0.0)),
            evaluation_time=evaluation_time or format_utc_timestamp(engram.clock()),
        )
        if not isinstance(rows, list):
            raise RuntimeError("vector Proposition search returned an invalid collection")
        return rows
    except (RuntimeError, ValueError) as err:
        logger.warning("Vector graph recall unavailable; using keyword fallback (%s)", failure_origin(err))
        result = []
        return result


def graph_vector_proposition_projections(
    engram,
    text: str,
    *,
    limit: int = 0,
    cooperative_check=(),
    max_working_memory_bytes: int = 0,
    basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
) -> list[dict]:
    """Return strictly decoded wire-safe ANN Proposition projections.

    ``basis_window`` is the request's Assertion basis window
    (``engram.evidence.assertion_basis_window``).
    """
    graph_config = engram.config.get("graph", {})
    if not engram.graph_client or not graph_config.get("enabled", False) or not graph_config.get("vector_enabled", False):
        result = []
        return result
    search = engram.graph_operation("vector_search_proposition_projections")
    row_limit = max(1, min(MAX_GRAPH_VECTOR_LIMIT, limit)) if limit else graph_config.get("vector_limit", 0)
    try:
        run_cooperative_check(cooperative_check)
        embedding = engram.encode_graph_query(text)
        require_working_memory(estimate_working_bytes(embedding), max_working_memory_bytes)
        run_cooperative_check(cooperative_check)
        rows = search(
            embedding,
            index_name=graph_config.get("vector_index_name", ""),
            limit=row_limit,
            min_similarity=float(graph_config.get("vector_min_similarity", 0.0)),
            basis_window=basis_window,
        )
        if not isinstance(rows, list) or len(rows) > row_limit:
            raise ValueError("vector Proposition projection boundary returned an invalid collection")
        validated_rows = [validate_proposition_projection(row) for row in rows]
        require_working_memory(
            estimate_working_bytes(embedding)
            + estimate_working_bytes([proposition_projection_to_dict(row) for row in validated_rows]),
            max_working_memory_bytes,
        )
        run_cooperative_check(cooperative_check)
        return validated_rows
    except (RuntimeError, ValueError) as err:
        # Only an ordinary model or graph failure omits the evidence; deadlines, the
        # request's memory budget, cancellation and programming errors propagate.
        logger.warning("Vector Proposition projection unavailable; omitting response-less evidence (%s)", failure_origin(err))
        result = []
        return result


def current_proposition_projection(
    engram, proposition_id: str, basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS
) -> tuple[dict, ...]:
    """Re-read one canonical Proposition through the fixed by-ID capability.

    ``basis_window`` selects the same request-eligible Assertion basis as discovery.
    """
    if not engram.graph_client:
        result = ()
        return result
    lookup = engram.graph_operation("proposition_projection_by_id")
    try:
        rows = lookup(proposition_id, basis_window)
    except RuntimeError as err:
        result = failed_graph_read("current_proposition_projection", err, ())
        return result
    if not isinstance(rows, list) or len(rows) > 1:
        raise ValueError("Proposition projection revalidation boundary returned an invalid collection")
    result = tuple(validate_proposition_projection(row) for row in rows)
    return result


def warm_vector_recall(engram) -> bool:
    """Load the query model and report whether the optional graph ANN path is ready."""
    graph_config = engram.config.get("graph", {})
    if not graph_config.get("vector_enabled", False):
        result = False
        return result
    if not engram.graph_client:
        raise RuntimeError("vector recall requires a graph client")
    search = engram.graph_operation("vector_search_propositions")
    embedding = engram.encode_graph_query("Engram vector recall readiness")
    search(
        embedding,
        index_name=graph_config.get("vector_index_name", ""),
        limit=1,
        min_similarity=0.0,
        evaluation_time=format_utc_timestamp(engram.clock()),
    )
    result = engram.graph_client_available()
    return result


def structured_proposition_projections(
    engram,
    text: str,
    *,
    row_limit: int,
    cooperative_check=(),
    max_working_memory_bytes: int = 0,
    basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
) -> list[dict]:
    """Run only fixed structured Proposition projections and reject conflicting rows."""
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or not 0 <= row_limit <= MAX_PROPOSITION_PROJECTION_ROWS:
        raise ValueError(f"row_limit must be an integer from 0 through {MAX_PROPOSITION_PROJECTION_ROWS}")
    if isinstance(max_working_memory_bytes, bool) or not isinstance(max_working_memory_bytes, int) or max_working_memory_bytes < 0:
        raise ValueError("max_working_memory_bytes must be a nonnegative integer")
    if not row_limit or not engram.graph_client:
        result = []
        return result
    search = engram.graph_operation("structured_proposition_projections")
    run_cooperative_check(cooperative_check)
    requests: list[tuple[PropositionProjectionQuery, str]] = []
    entities = extract_entities(text)
    if entities:
        requests.extend(
            (PropositionProjectionQuery.STRUCTURED_ENTITY, str(entity.get("text", "")))
            for entity in entities[:MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS]
        )
    else:
        keywords = extract_keywords(normalize(text), engram.config.get("stopwords", set()))
        requests.extend(
            (PropositionProjectionQuery.STRUCTURED_KEYWORD, keyword)
            for keyword in keywords[:MAX_STRUCTURED_PROPOSITION_PROJECTION_TERMS]
        )
    retained: dict[str, dict] = {}
    for projection_id, value in requests:
        run_cooperative_check(cooperative_check)
        remaining = row_limit - len(retained)
        if not remaining:
            break
        try:
            rows = search(value, projection_id=projection_id, limit=remaining, basis_window=basis_window)
        except RuntimeError as err:
            result = failed_graph_read("structured_proposition_projections", err, [])
            return result
        if not isinstance(rows, list) or len(rows) > remaining:
            raise ValueError("structured Proposition projection boundary returned an invalid collection")
        validated_rows = [validate_proposition_projection(row) for row in rows]
        for row in validated_rows:
            proposition_id = row.get("proposition_id", "")
            if proposition_id in retained and retained.get(proposition_id, {}) != row:
                raise ValueError(f"conflicting structured Proposition projections for Proposition ID: {proposition_id}")
            retained[proposition_id] = row
        run_cooperative_check(cooperative_check)
        require_working_memory(
            estimate_working_bytes([proposition_projection_to_dict(projection) for projection in retained.values()]),
            max_working_memory_bytes,
        )
    result = [retained.get(proposition_id, {}) for proposition_id in sorted(retained)]
    return result


def canonical_entity_matches(engram, surface: str, *, limit: int = MAX_RELATION_CANDIDATES, cooperative_check=()) -> list[dict]:
    """Resolve an entity surface through the fixed graph capability."""
    if not engram.graph_client:
        result = []
        return result
    search = engram.graph_operation("canonical_entity_matches")
    run_cooperative_check(cooperative_check)
    try:
        rows = search(surface, limit=limit)
    except RuntimeError as err:
        result = failed_graph_read("canonical_entity_matches", err, [])
        return result
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("canonical entity boundary returned an invalid collection")
    result = [canonical_entity_match_from_graph_row(row) for row in rows]
    run_cooperative_check(cooperative_check)
    return result


def canonical_predicate_matches(engram, surface: str, *, limit: int = MAX_RELATION_CANDIDATES, cooperative_check=()) -> list[dict]:
    """Resolve a Predicate surface through the fixed graph capability."""
    if not engram.graph_client:
        result = []
        return result
    search = engram.graph_operation("canonical_predicate_matches")
    run_cooperative_check(cooperative_check)
    try:
        rows = search(surface, limit=limit)
    except RuntimeError as err:
        result = failed_graph_read("canonical_predicate_matches", err, [])
        return result
    if not isinstance(rows, list) or len(rows) > limit:
        raise ValueError("canonical Predicate boundary returned an invalid collection")
    result = [canonical_predicate_match_from_graph_row(row) for row in rows]
    run_cooperative_check(cooperative_check)
    return result


def relation_one_hop_proposition_projections(
    engram,
    subject_entity_id: str,
    predicate_id: str,
    *,
    row_limit: int = MAX_RELATION_PLAN_ROWS,
    include_historical: bool = False,
    cooperative_check=(),
    max_working_memory_bytes: int = 0,
    basis_window: dict = UNCONSTRAINED_ASSERTION_BASIS,
) -> list[dict]:
    """Run the fixed one-hop Proposition template and enforce its output boundary."""
    if isinstance(row_limit, bool) or not isinstance(row_limit, int) or not 0 <= row_limit <= MAX_RELATION_PLAN_ROWS:
        raise ValueError(f"relation row_limit must be an integer from 0 through {MAX_RELATION_PLAN_ROWS}")
    if not isinstance(include_historical, bool):
        raise ValueError("relation include_historical must be a boolean")
    if not row_limit or not engram.graph_client:
        result = []
        return result
    search = engram.graph_operation("relation_one_hop_proposition_projections")
    run_cooperative_check(cooperative_check)
    try:
        rows = search(
            subject_entity_id, predicate_id, limit=row_limit, include_historical=include_historical, basis_window=basis_window
        )
    except RuntimeError as err:
        result = failed_graph_read("relation_one_hop_proposition_projections", err, [])
        return result
    if not isinstance(rows, list) or len(rows) > row_limit:
        raise ValueError("relation one-hop boundary returned an invalid collection")
    result = [validate_relation_proposition_projection(row) for row in rows]
    outside_binding = any(
        row.get("projection", {}).get("subject_entity_id", "") != subject_entity_id
        or row.get("projection", {}).get("predicate_id", "") != predicate_id
        for row in result
    )
    if outside_binding:
        raise ValueError("relation one-hop boundary returned a Proposition outside the requested canonical binding")
    # object_type and predicate_cardinality are StrEnum members, so str() is their wire value.
    require_working_memory(
        estimate_working_bytes(
            [
                {
                    "projection": proposition_projection_to_dict(row.get("projection", {})),
                    "object_label": row.get("object_label", ""),
                    "object_type": str(row.get("object_type", "")),
                    "predicate_cardinality": str(row.get("predicate_cardinality", "")),
                }
                for row in result
            ]
        ),
        max_working_memory_bytes,
    )
    run_cooperative_check(cooperative_check)
    return result


def graph_lookup(engram, text: str, *, evaluation_time: str = "") -> str:
    """Look up information in the knowledge graph based on input text.

    Extracts entities from the text and queries the graph for related
    information. Returns a natural language response if found.

    Args:
        text: User input text.
        evaluation_time: Optional canonical UTC evaluation timestamp. The
            shared runtime supplies this so every interface evaluates
            temporal graph assertions against the same request clock.

    Returns:
        Response string if graph has relevant info, otherwise an empty string.
    """
    client = engram.graph_client
    if not client:
        result = ""
        return result
    started_ns = time_monotonic_ns()
    selected_evaluation_time = evaluation_time or format_utc_timestamp(engram.clock())
    projection_timestamp(selected_evaluation_time, True, "graph lookup evaluation time")
    result = ""
    failed = False
    try:
        entities = extract_entities(text)
        facts = []
        if entities:
            # Query the canonical graph for each entity. The Proposition node
            # carries the rendered subject/predicate/object projection, so one
            # query covers the entity in either the subject or object role.
            for entity in entities:
                records = engram.graph_query(
                    GRAPH_ENTITY_FACTS_QUERY,
                    {"name": entity.get("text", ""), "evaluation_time": selected_evaluation_time},
                    raise_on_failure=True,
                )
                facts.extend(graph_records_to_facts(records))
        else:
            # Keyword fallback: match a canonical Entity whose primary label
            # contains a query keyword, then return its current surface triples.
            keywords = extract_keywords(normalize(text), engram.config.get("stopwords", set()))
            for keyword in keywords[:3]:
                records = engram.graph_query(
                    GRAPH_KEYWORD_FACTS_QUERY,
                    {"keyword": keyword, "evaluation_time": selected_evaluation_time},
                    raise_on_failure=True,
                )
                facts.extend(graph_records_to_facts(records))
        facts = graph_records_to_facts(
            [{"subject": subject, "predicate": predicate, "object": obj} for subject, predicate, obj in facts]
        )
        if facts:
            result = format_graph_facts(facts)
            return result
        vector_facts = graph_records_to_facts(
            engram.graph_vector_propositions(text, limit=5, evaluation_time=selected_evaluation_time)
        )
        result = format_graph_facts(vector_facts) if vector_facts else ""
        return result
    except RuntimeError as err:
        failed = True
        result = failed_graph_read("graph_lookup", err, "")
        return result
    finally:
        available = engram.graph_client_available()
        outcome = "failure" if failed or not available else "hit" if result else "miss"
        with engram.count_lock:
            record_graph_recall(engram.operational_metrics, outcome, time_monotonic_ns() - started_ns)
