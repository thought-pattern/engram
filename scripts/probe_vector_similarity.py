# Copyright 2025-2026 Thought Pattern AI, LLC
# SPDX-License-Identifier: Apache-2.0
"""Count the vector index's similarities above 1 on a configured graph, read-only (D-1257).

The fixed vector statements read a similarity from the cosine distance `vector_search.search` returns. For a query whose
embedding equals or nearly equals a stored one, that distance can fall a float32 epsilon below 0 from single-precision
arithmetic, so `1.0 - distance` lands above 1 and the projection codec refuses the row; the statements now read such a
distance as 0 (`VECTOR_SIMILARITY`). The probe takes the stored embeddings of `--queries` active Propositions spread
evenly by identifier as query vectors and prints counts only, all read through the fixed statements: the index metric,
the index rows of each query's `--window` nearest whose similarity exceeds 1, the projection rows above 1 and how many of
those are the query's own Proposition, the largest excess, and the queries whose projection read the codec refuses, both
as the first window alone (the read before D-1211) and widened. Before D-1257 the first two counts are nonzero and the
refusals follow; after it all are 0. Nothing is written.

`PYTHONPATH=component/engram/engram python3 component/engram/engram/scripts/probe_vector_similarity.py --config config.yml`
"""

from argparse import ArgumentParser as argparse_ArgumentParser
from json import dumps as json_dumps
from math import nextafter as math_nextafter

from engram.constants import DEFAULT_VECTOR_MIN_SIMILARITY, PROPOSITION_VECTOR_INDEX_NAME
from engram.constants_graph import VECTOR_INDEX_REACH_QUERY, VECTOR_PROPOSITION_PROJECTION_QUERY, PropositionProjectionQuery
from engram.errors import InvalidRequestError
from engram.graph import decode_projection_rows

from probe_vector_window import QUERY_SAMPLE_QUERY, graph_connection

INDEX_INFO_QUERY = "SHOW VECTOR INDEX INFO"
ABOVE_ONE = math_nextafter(1.0, 2.0)
PROJECTION_BASIS = {
    "basis_start": "",
    "basis_start_available": False,
    "basis_end": "",
    "basis_end_available": False,
    "basis_end_inclusive": False,
}


def index_metric(client) -> dict:
    """The Proposition vector index's metric and dimension as the graph reports them."""
    rows = client.execute(INDEX_INFO_QUERY, {})
    for row in rows:
        if row.get("index_name", "") == PROPOSITION_VECTOR_INDEX_NAME:
            result = {"metric": str(row.get("metric", "")), "dimension": row.get("dimension", 0)}
            return result
    result = {"metric": "", "dimension": 0}
    return result


def decodes(rows: list, limit: int) -> bool:
    """Whether the projection codec accepts the rows as the vector read decodes them."""
    try:
        decode_projection_rows(rows, PropositionProjectionQuery.VECTOR, PROPOSITION_VECTOR_INDEX_NAME, limit)
    except InvalidRequestError:
        return False
    return True


def graph_report(config_path: str, queries: int, window: int, asked: int) -> dict:
    """Count similarities above 1 among each query's nearest index rows and whether the projection read decodes."""
    client = graph_connection(config_path)
    try:
        samples = client.execute(QUERY_SAMPLE_QUERY, {"queries": queries})
        report = {
            "index": index_metric(client),
            "query_vectors": 0,
            "window": window,
            "asked": asked,
            "index_rows_above_one": 0,
            "queries_with_index_row_above_one": 0,
            "rows_read": 0,
            "rows_above_one": 0,
            "rows_above_one_own_proposition": 0,
            "queries_own_proposition_nearest": 0,
            "largest_excess": 0.0,
            "first_window_refused": 0,
            "widened_refused": 0,
        }
        for sample in samples:
            embedding = [float(value) for value in sample.get("embedding", []) or []]
            if not embedding:
                continue
            report["query_vectors"] = report.get("query_vectors", 0) + 1
            parameters = {"index_name": PROPOSITION_VECTOR_INDEX_NAME, "query_embedding": embedding}
            reach = client.execute(VECTOR_INDEX_REACH_QUERY, {**parameters, "window": window, "min_similarity": ABOVE_ONE})
            above_in_index = int(reach[0].get("reached", 0) or 0) if reach else 0
            report["index_rows_above_one"] = report.get("index_rows_above_one", 0) + above_in_index
            report["queries_with_index_row_above_one"] = report.get("queries_with_index_row_above_one", 0) + (1 if above_in_index else 0)
            nearest = client.execute(
                VECTOR_PROPOSITION_PROJECTION_QUERY,
                {**parameters, "min_similarity": DEFAULT_VECTOR_MIN_SIMILARITY, **PROJECTION_BASIS, "window": window, "limit": window},
            )
            above = [row for row in nearest if float(row.get("semantic_similarity", 0.0)) > 1.0]
            own = sample.get("proposition_id", "")
            report["rows_read"] = report.get("rows_read", 0) + len(nearest)
            report["rows_above_one"] = report.get("rows_above_one", 0) + len(above)
            report["rows_above_one_own_proposition"] = report.get("rows_above_one_own_proposition", 0) + sum(
                1 for row in above if row.get("proposition_id", "") == own
            )
            if nearest and nearest[0].get("proposition_id", "") == own:
                report["queries_own_proposition_nearest"] = report.get("queries_own_proposition_nearest", 0) + 1
            for row in above:
                report["largest_excess"] = max(report.get("largest_excess", 0.0), float(row.get("semantic_similarity", 0.0)) - 1.0)
            projection_parameters = {**parameters, "min_similarity": DEFAULT_VECTOR_MIN_SIMILARITY, **PROJECTION_BASIS}
            first_window = client.execute(VECTOR_PROPOSITION_PROJECTION_QUERY, {**projection_parameters, "window": asked, "limit": asked})
            if not decodes(first_window, asked):
                report["first_window_refused"] = report.get("first_window_refused", 0) + 1
            try:
                client.vector_search_proposition_projections(embedding, limit=asked, min_similarity=DEFAULT_VECTOR_MIN_SIMILARITY)
            except InvalidRequestError:
                report["widened_refused"] = report.get("widened_refused", 0) + 1
        return report
    finally:
        client.disconnect()


def main() -> None:
    """Print the configured graph's similarity report."""
    parser = argparse_ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--queries", type=int, default=20)
    parser.add_argument("--window", type=int, default=100)
    parser.add_argument("--asked", type=int, default=5)
    arguments = parser.parse_args()
    print(json_dumps(graph_report(arguments.config, arguments.queries, arguments.window, arguments.asked), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
