"""Existing graph transport fixture for retained adapter lifecycle harnesses."""

from engram.constants import ExpectedObjectType
from engram.graph import PropositionProjectionQuery, proposition_projection, relation_proposition_projection_from_graph_row


def proposition_row(proposition_id: str = "proposition:ada-birthplace", object_id: str = "entity:london") -> dict[str, object]:
    return {
        "proposition_id": proposition_id,
        "subject_entity_id": "entity:ada-lovelace",
        "predicate_id": "predicate:birth-place",
        "object_entity_id": object_id,
        "polarity": "positive",
        "modality_family": "none",
        "modality_operator": "none",
        "argument_count": 2,
        "qualification_count": 0,
        "context_count": 0,
        "applicability_count": 0,
        "invalidated_at": "",
        "invalidated_at_available": False,
        "system_from": "2026-01-01T00:00:00Z",
        "system_from_available": True,
        "system_to": "",
        "system_to_available": False,
        "valid_from": "",
        "valid_from_available": False,
        "valid_to": "",
        "valid_to_available": False,
        "predicate_canonical": True,
        "ownership_category": "PUBLIC",
        "trust_category": "",
        "trust_category_available": False,
        "supplied_trust": 0.8,
        "supplied_trust_available": True,
        "supplied_trust_version": 1,
        "supplied_trust_version_available": True,
        "structured_match": 1.0,
        "structured_match_available": True,
        "semantic_similarity": 0.0,
        "semantic_similarity_available": False,
    }


def internal_relation_result(
    proposition_id: str = "proposition:ada-birthplace",
    object_id: str = "entity:london",
    object_label: str = "London",
    object_type: str = "PLACE",
    predicate_cardinality: str = "SINGLE",
) -> dict:
    row = {
        **proposition_row(proposition_id, object_id),
        "object_label": object_label,
        "object_type": object_type,
        "predicate_cardinality": predicate_cardinality,
    }
    result = relation_proposition_projection_from_graph_row(row)
    return result


def internal_current(result: dict) -> dict:
    values = dict(result.get("projection", {}))
    values.update(
        {
            "projection_id": PropositionProjectionQuery.BY_ID,
            "structured_match": 0.0,
            "structured_match_available": False,
            "semantic_similarity": 0.0,
            "semantic_similarity_available": False,
            "vector_index_id": "",
            "vector_index_id_available": False,
        }
    )
    result = proposition_projection(**values)
    return result


def internal_entity_match(
    canonical_id: str = "entity:ada-lovelace",
    label: str = "Ada Lovelace",
) -> dict:
    result: dict = {
        "canonical_id": canonical_id,
        "primary_label": label,
        "aliases": ("Ada",),
        "edge_surfaces": ("Lovelace",),
        "entity_type": ExpectedObjectType.PERSON,
    }
    return result


def predicate_match(
    canonical_id: str = "predicate:birth-place",
    label: str = "birth place",
    object_type: ExpectedObjectType = ExpectedObjectType.PLACE,
) -> dict:
    result: dict = {
        "canonical_id": canonical_id,
        "primary_label": label,
        "synonyms": ("born", "born in"),
        "object_type": object_type,
    }
    return result


class RelationGraph:
    """Deterministic graph transport used by retained protocol harnesses."""

    available = True

    def __init__(self, results: tuple[dict, ...] = ()) -> None:
        self.results = list(results or (internal_relation_result(),))
        self.one_hop_calls: list[tuple[str, str, int, bool]] = []

    def canonical_entity_matches(self, surface, *, limit):
        result = [internal_entity_match()][:limit] if surface.casefold() in {"ada lovelace", "ada"} else []
        return result

    def canonical_predicate_matches(self, surface, *, limit):
        result = [predicate_match()][:limit] if surface.casefold() in {"born", "bear", "born in"} else []
        return result

    def relation_one_hop_proposition_projections(self, subject_entity_id, predicate_id, *, limit, include_historical=False):
        self.one_hop_calls.append((subject_entity_id, predicate_id, limit, include_historical))
        result = self.results[:limit]
        return result

    def proposition_projection_by_id(self, proposition_id):
        result = [
            internal_current(item) for item in self.results if item.get("projection", {}).get("proposition_id") == proposition_id
        ]
        return result
