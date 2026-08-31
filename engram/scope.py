"""Shared Tapestry/Engram graph visibility-scope contract."""


def validate_visibility_scope(value) -> dict:
    """Validate one global, company, or engagement graph scope."""
    if not isinstance(value, dict):
        raise ValueError("graph visibility_scope must be an object")
    scope = (
        dict(value)
        if value
        else {
            "kind": "global",
            "company_id": {},
            "customer_id": {},
            "engagement_id": {},
        }
    )
    if set(scope) != {"kind", "company_id", "customer_id", "engagement_id"}:
        raise ValueError("graph visibility_scope has an invalid shape")
    kind = scope.get("kind", "")
    company_id = scope.get("company_id", {})
    customer_id = scope.get("customer_id", {})
    engagement_id = scope.get("engagement_id", {})
    if kind == "global":
        if any(item != {} for item in (company_id, customer_id, engagement_id)):
            raise ValueError("global graph visibility requires empty identifiers")
    elif kind == "company":
        if not isinstance(company_id, str) or not company_id or customer_id != {} or engagement_id != {}:
            raise ValueError("company graph visibility requires only company_id")
    elif kind == "engagement":
        if not all(isinstance(item, str) and item for item in (company_id, customer_id, engagement_id)):
            raise ValueError("engagement graph visibility requires exact identifiers")
    else:
        raise ValueError("graph visibility_scope kind is unsupported")
    return scope


def visibility_parameters(value) -> dict:
    """Project a validated scope into primitive Memgraph query parameters."""
    scope = validate_visibility_scope(value)
    result = {
        "visibility_kind": scope.get("kind", "global"),
        "company_id": scope.get("company_id", "") if scope.get("company_id", {}) != {} else "",
        "customer_id": scope.get("customer_id", "") if scope.get("customer_id", {}) != {} else "",
        "engagement_id": scope.get("engagement_id", "") if scope.get("engagement_id", {}) != {} else "",
    }
    return result
