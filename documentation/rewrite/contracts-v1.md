# Symbolic retrieval rewrite contract v1

Status: component engineering contract, configuration-gated  
Owner: Section 11  
Release authority: Section 16 remains unprovisioned

## Boundary

The rewrite layer changes retrieval representations only. It does not change `QueryIdentity`, scope, temporal interpretation, required metadata, required source, eligibility time, or knowledge epoch. It cannot create or select response text and it does not call the AIML template processor.

`EngramCore.resolve_request` builds the base frame, performs Section 8 contextual enrichment, and then applies Section 11 rewrites when `retrieval_rewrites_enabled` is true. Contextual ordering permits a narrow rule to use an inherited subject while preserving its provenance. The resulting frame is passed to the ordinary resolver registry.

Exact, lexical, graph, composition, and semantic retrieval consume `resolved_text`, which is the final retrieval representation. The pattern resolver consumes the first trace input—the representation that existed before Section 11—or `resolved_text` when no rule applied. A rewritten exact hit does not short-circuit execution, so every other configured and eligible resolver still runs. These two controls prevent a rewrite from acting like an AIML redirect.

The feature is opt-in and defaults to false. Turning it off needs no persistence migration and does not make accepted response artifacts unreadable.

## Rule schema

The packaged corpus is `engram/data/rewrite-rules-v1.json`. The corpus and every rule use exact-key validation; unknown keys and unsupported schema versions fail during `EngramCore` construction when the feature is enabled.

Each rule contains:

- `rule_id` and positive `rule_version`;
- `category`;
- `input_constraints`: closed match mode, literal pattern, token bounds, optional closed operator allow-list, and an inherited-subject requirement;
- `output_template`: literal retrieval text with only the validated `{subject}` field permitted;
- integer `priority`;
- `scope`: `global` or `contextual`;
- bounded `max_applications`;
- provenance author, origin, license, and date.

Match modes are `exact`, `prefix`, `suffix`, and `token_sequence`. All are escaped literal matches. The engine accepts no regular expression, Python expression, callable, response body, AIML tag, or template instruction from the corpus. A contextual `{subject}` comes only from the already enriched frame and only when `inheritance` records the `subjects` field.

## Determinism and bounds

Rules are ordered by descending priority, then rule ID, then rule version. One highest-priority candidate applies per step. The engine enforces:

- maximum depth: 8 applications;
- maximum matching expansions: 16;
- maximum applications per rule: 8 schema maximum and a corpus-specific value;
- cycle rejection by previously observed case-folded form;
- output size at the existing 16,384-byte request limit;
- 50 ms operational rewrite deadline checked at deterministic step boundaries;
- the caller's transient cooperative cancellation check.

The time limit is an operational resource control, not a correctness or promotion threshold. Promotion reports observed durations only. The standalone engine returns the last complete representation and a concrete stop reason for diagnostics. The frame integration accepts only `fixed_point`; any depth, expansion, cycle, output, or time stop raises `RewriteLimitError` before resolver planning. Consequently, load or timing cannot silently select a different answer.

## QueryFrame trace

The existing schema-2 frame fields are sufficient and remain unchanged:

- `original_text` retains the caller request;
- `rewrite_chain` contains every applied `rule_id@rule_version`, input form, and output form;
- `resolved_text` contains the final retrieval representation.

The first trace input is also the non-rewritten pattern representation. Empty `rewrite_chain` means no Section 11 rule applied. Codecs retain the complete bounded chain. See `trace-fixtures-v1.json`.

## Failure and rollback

Malformed or unsupported package data fails eager initialization when enabled. A rewrite resource stop fails before resolver planning rather than using a partial chain, resolver execution remains fail-soft under its existing contracts, and caller cancellation propagates as cancellation. Corpus lint errors block component promotion. Runtime rollback is the single `retrieval_rewrites_enabled: false` setting; no accepted artifact, index, statement, or conversation record is deleted or rewritten.
