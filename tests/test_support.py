"""Support references are accepted on their shape, not on contract versions."""

from pytest import raises as pytest_raises

from engram.support import MAX_CONTRACT_NAME_BYTES, validate_support_reference

from .support_fixtures import ASSERTION_REFERENCE_A


def test_a_reference_naming_newer_contracts_is_accepted_and_kept_as_given() -> None:
    reference = {**ASSERTION_REFERENCE_A, "representation_contract": "representation-v2"}

    validated = validate_support_reference(reference)

    assert validated == reference


def test_contract_names_must_be_non_empty_and_bounded() -> None:
    with pytest_raises(ValueError, match="representation_contract"):
        validate_support_reference({**ASSERTION_REFERENCE_A, "representation_contract": "x" * (MAX_CONTRACT_NAME_BYTES + 1)})


def test_the_reference_shape_is_still_enforced() -> None:
    incomplete = {key: value for key, value in ASSERTION_REFERENCE_A.items() if key != "dependency_state_digest"}
    with pytest_raises(ValueError, match="invalid shape"):
        validate_support_reference(incomplete)
    # Record identifiers are opaque; record_kind implies no identifier prefix or length.
    opaque_id = "01999a4e-7c3b-7d2a-9f1e-3b5c7d9e1f20"
    assert validate_support_reference({**ASSERTION_REFERENCE_A, "id": opaque_id}).get("id", "") == opaque_id
