"""Existing scope inputs for the retained maintenance lifecycle harness."""

SCOPES = {
    "public": {"kind": "global", "company_id": {}, "customer_id": {}, "engagement_id": {}},
    "company": {"kind": "company", "company_id": "company-a", "customer_id": {}, "engagement_id": {}},
    "engagement_a": {
        "kind": "engagement",
        "company_id": "company-a",
        "customer_id": "customer-a",
        "engagement_id": "engagement-a",
    },
    "engagement_b": {
        "kind": "engagement",
        "company_id": "company-a",
        "customer_id": "customer-b",
        "engagement_id": "engagement-b",
    },
}
