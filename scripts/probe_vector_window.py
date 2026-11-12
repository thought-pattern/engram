"""Show Engram's vector reads returning the nearest passing facts (D-1211) and marking generic facts (D-1212), read-only.

Engram's two vector reads (`MemGraphConnection.vector_search_propositions` and
`vector_search_proposition_projections`) ask the index for a window of nearest Propositions and filter them after;
`nearest_passing_rows` widens the window until the asked number pass or the index holds no further row at the
similarity floor. A generic predicate's fact (generic_relation, generic_event) carries a nonempty `predicate_surface`,
and the projection reads `predicate_canonical` false for it, so the evidence decision refuses it as retrieval-only.
Engram's fact recall phrases a generic fact with that surface, not the generic Predicate's label (D-1256).

Two modes, both printing counts only:

- With no `--config`: a stand-in index with no graph. 400 Propositions in falling similarity, every fourth of a shape the
  reads keep, the last 100 below the floor. Each read asks for 5, 50 and 100 facts; the first window alone (the read
  before D-1211) and the widened read are counted, with the index reads each made. Recall wording: three stand-in facts,
  two generic with their own surface and one curated, phrased as the fact-recall return reads them before D-1256 (the
  Predicate's label) and now.
- With `--config` naming a Tapestry config.yml: its `memgraph` section opens a read-only Engram connection to that graph.
  The query vectors are the stored embeddings of `--queries` active Propositions spread evenly by identifier. For each
  query and each asked count the probe reports, for both reads, the rows the first window alone returns (the read before
  D-1211) and the rows the widened read returns, and among each the generic facts (nonempty `predicate_surface`); for
  the projection read it also counts generic rows marked `predicate_canonical` true (0 after D-1212; before it every
  row was marked true). It also counts, among the widened facts, the generic ones the recall return words with the
  generic Predicate's label (0 after D-1256; before it every generic fact was). Nothing is written.

`PYTHONPATH=component/engram/engram python3 component/engram/engram/scripts/probe_vector_window.py [--config config.yml]`
"""

from argparse import ArgumentParser as argparse_ArgumentParser
from datetime import datetime, timezone
from json import dumps as json_dumps
from pathlib import Path

from yaml import safe_load as yaml_safe_load

from engram.constants import DEFAULT_VECTOR_MIN_SIMILARITY, PROPOSITION_VECTOR_INDEX_NAME
from engram.constants_graph import (
    GRAPH_FACT_PREDICATE_TEXT,
    GRAPH_FACT_RETURN,
    VECTOR_INDEX_REACH_QUERY,
    VECTOR_PROPOSITION_PROJECTION_QUERY,
    VECTOR_SEARCH_PROPOSITIONS_QUERY,
)
from engram.graph import MemGraphConnection, nearest_passing_rows
from engram.phrasing import phrase_fact

ASKED_COUNTS = (5, 50, 100)
STAND_IN_SIZE = 400
STAND_IN_BELOW_FLOOR = 100
QUERY_SAMPLE_QUERY = (
    "MATCH (proposition:Proposition) WHERE proposition.embedding IS NOT NULL "
    "AND proposition.lifecycle_disposition = 'active' AND proposition.retired_at IS NULL "
    "WITH proposition ORDER BY proposition.id "
    "WITH collect(proposition) AS propositions "
    "WITH propositions, size(propositions) AS total "
    "UNWIND range(0, $queries - 1) AS position "
    "WITH propositions[toInteger(position * total / $queries)] AS proposition WHERE proposition IS NOT NULL "
    "RETURN DISTINCT proposition.id AS proposition_id, proposition.embedding AS embedding"
)
GENERIC_COUNT_QUERY = (
    "MATCH (proposition:Proposition) WHERE proposition.id IN $proposition_ids "
    "RETURN count(CASE WHEN coalesce(proposition.predicate_surface, '') <> '' THEN 1 END) AS generic"
)
LABEL_WORDED_QUERY = (
    "MATCH (proposition:Proposition)-[:USES_PREDICATE]->(predicate:Predicate) WHERE proposition.id IN $proposition_ids "
    "AND coalesce(proposition.predicate_surface, '') <> '' "
    "RETURN count(CASE WHEN coalesce(predicate.label, predicate.canonical_id) = " + GRAPH_FACT_PREDICATE_TEXT + " THEN 1 END) "
    "AS label_worded"
)
# The generic relation's label in the predicate catalog (data/predicate_vocabulary.yaml).
GENERIC_LABEL = "generic relation"
WORDING_FACTS = (
    {"subject": "Głogów", "predicate_surface": "lies on", "label": GENERIC_LABEL, "object": "the Oder"},
    {"subject": "the ferry", "predicate_surface": "sails from", "label": GENERIC_LABEL, "object": "Brzeg"},
    {"subject": "Ada Lovelace", "predicate_surface": "", "label": "was born in", "object": "London"},
)
GENERIC_IDS_QUERY = (
    "MATCH (proposition:Proposition) WHERE proposition.id IN $proposition_ids "
    "AND coalesce(proposition.predicate_surface, '') <> '' RETURN proposition.id AS proposition_id"
)


def stand_in_index() -> list:
    """Propositions in falling similarity; every fourth passes the reads' filters; the last 100 sit below the floor."""
    rows = []
    for position in range(STAND_IN_SIZE):
        below = position >= STAND_IN_SIZE - STAND_IN_BELOW_FLOOR
        similarity = DEFAULT_VECTOR_MIN_SIMILARITY - 0.01 if below else 0.99 - position * 0.0015
        rows.append({"proposition_id": f"p{position:04d}", "similarity": similarity, "passes": position % 4 == 0})
    return rows


def stand_in_execute(index: list, reads: list):
    """Answer the fixed vector statements as the graph does over the stand-in index, recording each statement."""

    def execute(query: str, parameters=()) -> list:
        values = dict(parameters) if isinstance(parameters, dict) else {}
        window = values.get("window", 0)
        floor = values.get("min_similarity", 0.0)
        nearest = [row for row in index[:window] if row.get("similarity", 0.0) >= floor]
        if query == VECTOR_INDEX_REACH_QUERY:
            reads.append("reach")
            result = [{"reached": len(nearest)}]
            return result
        reads.append("filtered")
        passing = [row for row in nearest if row.get("passes", False)]
        result = passing[: values.get("limit", 0)] if "limit" in values else passing
        return result

    return execute


def recall_predicate(fact: dict, statement: str) -> str:
    """The predicate words a stand-in fact reads as under a fact-recall return, as the graph evaluates it."""
    surface = fact.get("predicate_surface", "")
    words = surface if GRAPH_FACT_PREDICATE_TEXT in statement and surface else fact.get("label", "")
    return words


def wording_report() -> dict:
    """Phrase the stand-in facts as the recall return read them before D-1256 (the Predicate's label) and now."""
    before_statement = "coalesce(predicate.label, predicate.canonical_id) AS predicate"
    report = {"facts": len(WORDING_FACTS), "generic": sum(1 for fact in WORDING_FACTS if fact.get("predicate_surface", ""))}
    for name, statement in (("before", before_statement), ("now", GRAPH_FACT_RETURN)):
        sentences = [
            phrase_fact(fact.get("subject", ""), recall_predicate(fact, statement), fact.get("object", "")) for fact in WORDING_FACTS
        ]
        report[f"{name}_worded_by_generic_label"] = sum(1 for sentence in sentences if GENERIC_LABEL in sentence)
        report[f"{name}_worded_by_own_surface"] = sum(
            1
            for fact, sentence in zip(WORDING_FACTS, sentences)
            if fact.get("predicate_surface", "") and fact.get("predicate_surface", "") in sentence
        )
        report[f"{name}_curated_by_label"] = sum(
            1
            for fact, sentence in zip(WORDING_FACTS, sentences)
            if not fact.get("predicate_surface", "") and fact.get("label", "") in sentence
        )
    return report


def stand_in_report() -> dict:
    """Count asked, first-window and widened rows over the stand-in index for both statements."""
    index = stand_in_index()
    passing_total = sum(
        1 for row in index if row.get("passes", False) and row.get("similarity", 0.0) >= DEFAULT_VECTOR_MIN_SIMILARITY
    )
    report = {"index_rows": len(index), "passing_at_or_above_floor": passing_total, "reads": [], "recall_wording": wording_report()}
    for statement_name, statement in (("facts", VECTOR_SEARCH_PROPOSITIONS_QUERY), ("projections", VECTOR_PROPOSITION_PROJECTION_QUERY)):
        for asked in ASKED_COUNTS:
            reads = []
            client = MemGraphConnection()
            client.execute = stand_in_execute(index, reads)
            parameters = {
                "index_name": PROPOSITION_VECTOR_INDEX_NAME,
                "query_embedding": [0.0, 1.0],
                "min_similarity": DEFAULT_VECTOR_MIN_SIMILARITY,
            }
            first_window = client.execute(statement, {**parameters, "window": asked, "limit": asked})
            reads.clear()
            widened = nearest_passing_rows(client, statement, parameters, asked)
            ordered = [row.get("proposition_id", "") for row in widened] == sorted(row.get("proposition_id", "") for row in widened)
            report.get("reads", []).append(
                {
                    "statement": statement_name,
                    "asked": asked,
                    "first_window_returned": len(first_window),
                    "widened_returned": len(widened),
                    "widened_nearest_first": ordered,
                    "filtered_reads": reads.count("filtered"),
                    "reach_reads": reads.count("reach"),
                }
            )
    return report


def graph_connection(config_path: str) -> MemGraphConnection:
    """Open a read-only Engram connection from a Tapestry config's memgraph section."""
    loaded = yaml_safe_load(Path(config_path).read_text(encoding="utf-8"))
    memgraph = loaded.get("memgraph", {}) if isinstance(loaded, dict) else {}
    if not isinstance(memgraph, dict) or not memgraph:
        raise ValueError("the config has no memgraph section")
    client = MemGraphConnection(
        host=str(memgraph.get("host", "") or ""),
        port=int(memgraph.get("port", 0) or 0),
        username=str(memgraph.get("username", "") or ""),
        password=str(memgraph.get("password", "") or ""),
    )
    if not client.connect():
        raise RuntimeError("the graph database is unavailable")
    return client


def generic_count(client: MemGraphConnection, proposition_ids: list) -> int:
    """How many of the given Propositions carry a generic predicate's surface."""
    rows = client.execute(GENERIC_COUNT_QUERY, {"proposition_ids": proposition_ids}) if proposition_ids else []
    count = int(rows[0].get("generic", 0) or 0) if rows else 0
    return count


def label_worded(client: MemGraphConnection, proposition_ids: list) -> int:
    """How many of the given generic facts the recall return words with their Predicate's label."""
    rows = client.execute(LABEL_WORDED_QUERY, {"proposition_ids": proposition_ids}) if proposition_ids else []
    count = int(rows[0].get("label_worded", 0) or 0) if rows else 0
    return count


def graph_report(config_path: str, queries: int) -> dict:
    """Count asked, first-window and widened rows, and generic facts among them, on the configured graph."""
    client = graph_connection(config_path)
    try:
        samples = client.execute(QUERY_SAMPLE_QUERY, {"queries": queries})
        evaluation_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        totals = {}
        for sample in samples:
            embedding = [float(value) for value in sample.get("embedding", []) or []]
            if not embedding:
                continue
            for asked in ASKED_COUNTS:
                fact_parameters = {
                    "index_name": PROPOSITION_VECTOR_INDEX_NAME,
                    "query_embedding": embedding,
                    "min_similarity": DEFAULT_VECTOR_MIN_SIMILARITY,
                    "evaluation_time": evaluation_time,
                }
                first_facts = client.execute(VECTOR_SEARCH_PROPOSITIONS_QUERY, {**fact_parameters, "window": asked, "limit": asked})
                facts = client.vector_search_propositions(
                    embedding, evaluation_time=evaluation_time, limit=asked, min_similarity=DEFAULT_VECTOR_MIN_SIMILARITY
                )
                projection_parameters = {
                    "index_name": PROPOSITION_VECTOR_INDEX_NAME,
                    "query_embedding": embedding,
                    "min_similarity": DEFAULT_VECTOR_MIN_SIMILARITY,
                    "basis_start": "",
                    "basis_start_available": False,
                    "basis_end": "",
                    "basis_end_available": False,
                    "basis_end_inclusive": False,
                }
                first_projections = client.execute(
                    VECTOR_PROPOSITION_PROJECTION_QUERY, {**projection_parameters, "window": asked, "limit": asked}
                )
                projections = client.vector_search_proposition_projections(
                    embedding, limit=asked, min_similarity=DEFAULT_VECTOR_MIN_SIMILARITY
                )
                projection_ids = [row.get("proposition_id", "") for row in projections]
                generic_projection_ids = set()
                if projection_ids:
                    rows = client.execute(GENERIC_IDS_QUERY, {"proposition_ids": projection_ids})
                    generic_projection_ids = {row.get("proposition_id", "") for row in rows}
                counts = {
                    "queries": 1,
                    "asked": asked,
                    "facts_first_window": len(first_facts),
                    "facts_widened": len(facts),
                    "facts_first_window_generic": generic_count(client, [row.get("proposition_id", "") for row in first_facts]),
                    "facts_widened_generic": generic_count(client, [row.get("proposition_id", "") for row in facts]),
                    "projections_first_window": len(first_projections),
                    "projections_widened": len(projections),
                    "projections_first_window_generic": generic_count(
                        client, [row.get("proposition_id", "") for row in first_projections]
                    ),
                    "projections_widened_generic": len(generic_projection_ids),
                    "projections_widened_generic_marked_canonical": sum(
                        1
                        for row in projections
                        if row.get("proposition_id", "") in generic_projection_ids and row.get("predicate_canonical", False)
                    ),
                    "projections_widened_marked_retrieval_only": sum(1 for row in projections if not row.get("predicate_canonical", False)),
                    "facts_widened_generic_worded_by_label": label_worded(client, [row.get("proposition_id", "") for row in facts]),
                }
                current = totals.get(asked, {})
                totals[asked] = {name: current.get(name, 0) + value if name != "asked" else value for name, value in counts.items()}
        report = {"query_vectors": len(samples), "by_asked": [totals.get(asked, {}) for asked in ASKED_COUNTS]}
        return report
    finally:
        client.disconnect()


def main() -> None:
    """Print the stand-in report, or the configured graph's report when a config is named."""
    parser = argparse_ArgumentParser()
    parser.add_argument("--config", default="")
    parser.add_argument("--queries", type=int, default=20)
    arguments = parser.parse_args()
    report = graph_report(arguments.config, arguments.queries) if arguments.config else stand_in_report()
    print(json_dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
