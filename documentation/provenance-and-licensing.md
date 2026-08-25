# Retrieval provenance and licensing

**Status:** Current reviewed inventory

Engram source code, packaged rewrite rules, and repository-authored evaluation
corpora are licensed under the repository Apache-2.0 license.

The Engram-authored retrieval-rewrite corpus contains bounded literal request
transformations. Its evaluation corpus is separate repository material used by the
evaluation command.

The fielded BM25 implementation, tokenizer, documents, and comparison queries are
project-owned Python and data. SQLite FTS5 and Python's standard library served as
local comparison implementations. Sparse retrieval runs in process.

Standalone semantic retrieval uses the Apache-2.0
`sentence-transformers/all-MiniLM-L6-v2` model at immutable revision
`826711e54e001c83835913827a843d8dd0a1def9`. The provisioning command records and
verifies the model payload checksum and license. Serving uses only the provisioned
local artifact. This revision is the approved semantic artifact.

Any replacement corpus, model, tokenizer, backend, or learned scorer requires its
own immutable identity, license review, reproducible provisioning or source record,
and qualification evidence before it is enabled.
