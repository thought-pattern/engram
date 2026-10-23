"""Candidate discovery over an Engram's accepted-response artifacts.

Request-local sparse and standalone semantic search, and ranking of scoped cached
responses through their graph support Propositions. Each function that takes ``engram``
is bound on ``engram.core.Engram`` as the method of the same name.
"""

from heapq import heappush as heapq_heappush, heapreplace as heapq_heapreplace

from engram.constants import DEFAULT_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT
from engram.errors import VectorSupportScanExceededError
from engram.resources import estimate_working_bytes, require_working_memory, run_cooperative_check
from engram.sparse import search_sparse_artifacts


def sparse_candidates(engram, text: str, scope, *, limit: int, max_working_memory_bytes: int) -> dict:
    """Search request-local sparse structures derived from the current artifacts."""
    artifacts = engram.response_repository.trusted_artifacts()
    result = search_sparse_artifacts(
        tuple(artifacts.values()),
        text,
        scope,
        engram.config.get("sparse", {}),
        limit=limit,
        max_working_memory_bytes=max_working_memory_bytes,
        trusted_artifacts=True,
        index=engram.sparse_index,
    )
    return result


def semantic_candidates(
    engram, text: str, scope, *, limit: int, max_vector_results: int, max_working_memory_bytes: int, cooperative_check=()
) -> dict:
    """Search request-local embeddings derived from the current artifacts."""
    artifacts = engram.response_repository.trusted_artifacts()
    engram.semantic_retriever.retain(artifacts)
    result = engram.semantic_retriever.search(
        text,
        scope,
        tuple(artifacts.values()),
        limit=limit,
        max_vector_results=max_vector_results,
        max_working_memory_bytes=max_working_memory_bytes,
        cooperative_check=cooperative_check,
        trusted_artifacts=True,
    )
    return result


def vector_supported_matches(engram, text: str, *, limit: int, artifact_filter=()) -> list[tuple[dict, float]]:
    """Rank scoped cached responses through their KG support Propositions."""
    result = [
        (match.get("artifact", {}), match.get("retrieval_score", 0.0))
        for match in engram.vector_supported_match_components(text, limit=limit, artifact_filter=artifact_filter)
    ]
    return result


def vector_supported_match_components(
    engram, text: str, *, limit: int, artifact_filter=(), cooperative_check=(), max_working_memory_bytes: int = 0
) -> list[dict]:
    """Return support matches with raw similarity separate from response ranking."""
    run_cooperative_check(cooperative_check)
    rows = engram.graph_vector_propositions(text, limit=limit)
    result = engram.vector_supported_proposition_match_components(
        rows,
        limit=limit,
        artifact_filter=artifact_filter,
        cooperative_check=cooperative_check,
        max_working_memory_bytes=max_working_memory_bytes,
    )
    return result


def vector_supported_proposition_match_components(
    engram, rows: list[dict], *, limit: int, artifact_filter=(), cooperative_check=(), max_working_memory_bytes: int = 0
) -> list[dict]:
    """Intersect discovered Proposition hits with typed response support."""
    run_cooperative_check(cooperative_check)
    require_working_memory(estimate_working_bytes(rows), max_working_memory_bytes)
    support_scores: dict[str, float] = {}
    for row in rows:
        proposition_id = row.get("proposition_id", "")
        if not proposition_id:
            continue
        # A graph row may carry a null or zero similarity; both score as 0.0.
        similarity = row.get("similarity", 0.0)
        support_scores[str(proposition_id)] = float(similarity) if similarity else 0.0
    result = engram.vector_supported_match_components_from_scores(
        support_scores,
        source_working_bytes=estimate_working_bytes(rows),
        limit=limit,
        artifact_filter=artifact_filter,
        cooperative_check=cooperative_check,
        max_working_memory_bytes=max_working_memory_bytes,
    )
    return result


def vector_supported_match_components_from_scores(
    engram,
    support_scores: dict[str, float],
    *,
    source_working_bytes: int,
    limit: int,
    artifact_filter=(),
    cooperative_check=(),
    max_working_memory_bytes: int = 0,
) -> list[dict]:
    """Rank scoped cached responses by their best supporting Proposition similarity.

    Raises ``VectorSupportScanExceededError`` when the matching support references exceed
    ``graph.vector_support_scan_limit`` and ``WorkingMemoryExceededError`` past the
    request's working-memory budget, so a truncated scan never reads as a miss.
    """
    if not support_scores:
        result = []
        return result
    graph_settings = engram.config.get("graph", {})
    vector_weight = float(graph_settings.get("vector_weight", 0.0))
    scan_limit = int(graph_settings.get("vector_support_scan_limit", DEFAULT_GRAPH_VECTOR_SUPPORT_SCAN_LIMIT))
    if limit < 1:
        result = []
        return result
    scored: list[tuple[float, int, dict]] = []
    scored_bytes = 64
    retained_bytes = source_working_bytes + estimate_working_bytes(support_scores)
    repository = engram.response_repository.trusted_state()
    edge_count = 0
    for index, artifact in enumerate(repository.get("artifacts", {}).values()):
        run_cooperative_check(cooperative_check)
        if artifact_filter and not artifact_filter(artifact):
            continue
        similarities = []
        for reference in artifact.get("support_references", ()):
            reference_id = reference.get("id", "")
            if reference_id not in support_scores:
                continue
            edge_count += 1
            if edge_count > scan_limit:
                raise VectorSupportScanExceededError(
                    f"relevant response support fan-out exceeds the configured limit of {scan_limit}"
                )
            similarities.append(support_scores.get(reference_id, 0.0))
        if not similarities:
            continue
        retained_bytes += estimate_working_bytes(artifact.get("statement_id", ""))
        require_working_memory(retained_bytes, max_working_memory_bytes)
        semantic_similarity = max(similarities)
        priority = 0.0
        score = semantic_similarity * vector_weight
        if len(scored) >= limit and (score, index) <= (scored[0][0], scored[0][1]):
            continue
        components = {
            "artifact": artifact,
            "retrieval_score": score,
            "semantic_similarity": semantic_similarity,
            "vector_weight": vector_weight,
            "priority": priority,
        }
        ranked = (score, index, components)
        if len(scored) < limit:
            ranked_bytes = estimate_working_bytes(ranked)
            heapq_heappush(scored, ranked)
            scored_bytes += ranked_bytes
        else:
            ranked_bytes = estimate_working_bytes(ranked)
            removed = heapq_heapreplace(scored, ranked)
            scored_bytes += ranked_bytes - estimate_working_bytes(removed)
        require_working_memory(retained_bytes + scored_bytes, max_working_memory_bytes)
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    run_cooperative_check(cooperative_check)
    result = [components for _, _, components in scored]
    return result
