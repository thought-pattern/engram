"""Support references are accepted on their exact shape."""

from pytest import raises as pytest_raises

from engram.support import validate_support_reference
from tests.support_fixtures import ASSERTION_REFERENCE_A


def test_the_reference_shape_is_enforced() -> None:
    incomplete = {key: value for key, value in ASSERTION_REFERENCE_A.items() if key != "dependency_state_digest"}
    with pytest_raises(ValueError, match="invalid shape"):
        validate_support_reference(incomplete)
    with pytest_raises(ValueError, match="invalid shape"):
        validate_support_reference({**ASSERTION_REFERENCE_A, "unsupported_field": 1})
    # Record identifiers are opaque; record_kind implies no identifier prefix or length.
    opaque_id = "01999a4e-7c3b-7d2a-9f1e-3b5c7d9e1f20"
    assert validate_support_reference({**ASSERTION_REFERENCE_A, "id": opaque_id}).get("id", "") == opaque_id
