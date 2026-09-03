"""Probe Section 10 through fixed capabilities against the configured live MemGraph."""

from argparse import ArgumentParser as argparse_ArgumentParser
from json import dumps as json_dumps
from pathlib import Path
from sys import path as sys_path
from time import perf_counter_ns as time_perf_counter_ns

REPOSITORY = Path(__file__).resolve().parents[1]
if str(REPOSITORY) not in sys_path:
    sys_path.insert(0, str(REPOSITORY))

from engram.config import load_config
from engram.constants import ResolutionOutcome
from engram.core import Engram
from engram.errors import InvalidRequestError
from engram.resolution import resolver_result_to_dict, validate_resolution_result
from engram.service import EngramCore

PREDICATE_SURFACES = ("married", "spouse", "husband", "present", "present in", "work", "born", "parent")
COMPOSITION_PROMPTS = ("Who is Sarah's married partner married to?",)


def timed(operation):
    started = time_perf_counter_ns()
    value = operation()
    result = (value, (time_perf_counter_ns() - started) / 1_000_000)
    return result


def run(config_path: str) -> dict[str, object]:
    engine = Engram(load_config(config_path))
    try:
        entities, entity_ms = timed(lambda: engine.canonical_entity_matches("Sarah", limit=4))
        predicate_rows = []
        for surface in PREDICATE_SURFACES:
            rows, elapsed_ms = timed(lambda surface=surface: engine.canonical_predicate_matches(surface, limit=4))
            predicate_rows.append(
                {
                    "surface": surface,
                    "elapsed_ms": elapsed_ms,
                    "matches": [
                        {
                            "canonical_id": row["canonical_id"],
                            "primary_label": row["primary_label"],
                            "object_type": row["object_type"].value,
                        }
                        for row in rows
                    ],
                }
            )
        predicates = {row["canonical_id"]: row for surface in predicate_rows for row in surface["matches"] if isinstance(row, dict)}
        one_hop = []
        second_hop_subjects = {}
        if len(entities) == 1:
            for predicate_id in sorted(predicates):
                rows, elapsed_ms = timed(
                    lambda predicate_id=predicate_id: engine.relation_one_hop_proposition_projections(
                        entities[0]["canonical_id"],
                        predicate_id,
                        row_limit=4,
                    )
                )
                one_hop.append(
                    {
                        "subject_entity_id": entities[0]["canonical_id"],
                        "predicate_id": predicate_id,
                        "elapsed_ms": elapsed_ms,
                        "propositions": [
                            {
                                "proposition_id": row["projection"]["proposition_id"],
                                "object_entity_id": row["projection"]["object_entity_id"],
                                "object_label": row["object_label"],
                                "object_type": row["object_type"].value,
                            }
                            for row in rows
                        ],
                    }
                )
                for row in rows:
                    second_hop_subjects[row["projection"]["object_entity_id"]] = row["object_label"]
        two_hop = []
        for subject_id in sorted(second_hop_subjects)[:4]:
            for predicate_id in sorted(predicates):
                rows, elapsed_ms = timed(
                    lambda subject_id=subject_id, predicate_id=predicate_id: engine.relation_one_hop_proposition_projections(
                        subject_id,
                        predicate_id,
                        row_limit=4,
                    )
                )
                if rows:
                    two_hop.append(
                        {
                            "subject_entity_id": subject_id,
                            "subject_label": second_hop_subjects.get(subject_id, ""),
                            "predicate_id": predicate_id,
                            "elapsed_ms": elapsed_ms,
                            "propositions": [
                                {
                                    "proposition_id": row["projection"]["proposition_id"],
                                    "object_entity_id": row["projection"]["object_entity_id"],
                                    "object_label": row["object_label"],
                                    "object_type": row["object_type"].value,
                                }
                                for row in rows
                            ],
                        }
                    )
        core = EngramCore(engine)
        resolutions = []
        for index, prompt in enumerate(COMPOSITION_PROMPTS):
            result, elapsed_ms = timed(
                lambda index=index, prompt=prompt: core.resolve_request(
                    prompt,
                    f"live-composition-{index}",
                    user_id="Sarah",
                    configured_resolvers=("structured_graph",),
                )
            )
            validated_result = validate_resolution_result(result)
            evidence_package = validated_result.get("evidence_package", {})
            if not isinstance(evidence_package, dict):
                raise InvalidRequestError("resolution evidence package is malformed")
            evidence_records = evidence_package.get("records", ())
            if not isinstance(evidence_records, tuple):
                raise InvalidRequestError("resolution evidence records are malformed")
            structured = next(
                resolver
                for resolver in validated_result.get("resolver_results", ())
                if resolver.get("resolver", "") == "structured_graph"
            )
            structured_payload = resolver_result_to_dict(structured)
            resolutions.append(
                {
                    "prompt": prompt,
                    "elapsed_ms": elapsed_ms,
                    "outcome": validated_result.get("outcome", ResolutionOutcome.MISS).value,
                    "responses": [candidate.get("response", "") for candidate in validated_result.get("response_candidates", ())],
                    "evidence_paths": [
                        [step.get("proposition_id", "") for step in record.get("path", ()) if isinstance(step, dict)]
                        for record in evidence_records
                    ],
                    "structured_reason": structured.get("reason_code", ""),
                    "structured_diagnostics": structured_payload.get("diagnostics", {}),
                    "graph_rows": structured.get("consumption", {}).get("graph_rows", 0),
                }
            )
        result = {
            "schema_version": 1,
            "config_path": config_path,
            "entity_lookup": {
                "elapsed_ms": entity_ms,
                "matches": [
                    {
                        "canonical_id": row["canonical_id"],
                        "primary_label": row["primary_label"],
                        "entity_type": row["entity_type"].value,
                    }
                    for row in entities
                ],
            },
            "predicate_lookups": predicate_rows,
            "one_hop": one_hop,
            "two_hop": two_hop,
            "core_resolutions": resolutions,
            "timing_gate": False,
        }
        return result
    finally:
        disconnect = getattr(engine.graph_client, "disconnect", False)
        if callable(disconnect):
            disconnect()


def main() -> None:
    parser = argparse_ArgumentParser()
    parser.add_argument("--config", default="config.yml")
    parser.add_argument("--output", type=Path)
    arguments = parser.parse_args()
    report = run(arguments.config)
    if arguments.output:
        arguments.output.parent.mkdir(parents=True, exist_ok=True)
        arguments.output.write_text(json_dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json_dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
