"""Thread-safe process-memory feedback ingestion, receipts and history ownership."""

from datetime import timedelta
from threading import RLock as threading_RLock

from engram.constants_feedback import (
    FEEDBACK_OUTCOME_COUNTER_FIELDS,
    MAX_FEEDBACK_OBSERVATIONS,
    MAX_INSPECTION_RECORDS,
    MAX_REFERENCE_ID_BYTES,
    MAX_STATEMENT_ID_BYTES,
    FeedbackOutcome,
    LifecycleHandoffStatus,
)
from engram.constants_identity import MAX_NAMESPACE_BYTES
from engram.constants_mutations import MutationOperation, MutationResultCode, ReceiptCompletionState, ReceiptLookupOutcome
from engram.errors import ConflictError, IdentityValidationError, InvalidRequestError
from engram.feedback import (
    feedback_bucket,
    feedback_history,
    feedback_payload_signature,
    feedback_policy_fingerprint,
    feedback_state,
    feedback_statistics,
    feedback_statistics_increment,
    feedback_statistics_to_dict,
    freeze_feedback_record,
    policy_suppression,
    policy_suppression_signature,
    relationship_feedback_key_fingerprint,
    stale_exclusion,
    stale_exclusion_signature,
    statement_feedback_key_fingerprint,
    trusted_feedback_key_fingerprint,
    trusted_feedback_record_apply,
    trusted_feedback_state_copy,
    validate_feedback_bucket,
    validate_feedback_observation,
    validate_feedback_policy,
    validate_feedback_state,
)
from engram.feedback_values import bucket_start, internal_diagnostic_id, internal_fingerprint, internal_integer
from engram.identity import validate_query_identity
from engram.mutations import MutationReceiptLedger, mutation_receipt, receipt_lookup_receipt, validate_mutation_receipt
from engram.validation import require_bool, require_text, require_utc_datetime, utc_datetime


def trusted_feedback_record_from_observation(key: dict, observation: dict, policy: dict) -> dict:
    """Start a statement or relationship aggregate from its first store-validated observation."""
    outcome = observation.get("outcome", FeedbackOutcome.CANDIDATE)
    observed_at = observation.get("observed_at", "")
    statistics = feedback_statistics_increment(feedback_statistics(), outcome)
    bucket = feedback_bucket(bucket_start(observed_at, policy.get("bucket_seconds", 0)), statistics)
    result: dict = {
        "key": key,
        "raw": statistics,
        "buckets": (bucket,),
        "last_outcome": outcome,
        "last_observed_at": observed_at,
    }
    return result


def record_buckets(value: object) -> tuple[dict, ...]:
    if not isinstance(value, dict) or not isinstance(value.get("buckets", ()), tuple):
        raise InvalidRequestError("feedback record must contain a tuple of buckets")
    result = tuple(validate_feedback_bucket(bucket) for bucket in value.get("buckets", ()))
    return result


def aged_values(records: tuple[object, ...], at: str, policy: dict) -> dict[FeedbackOutcome, float]:
    evaluation = require_utc_datetime(at, "feedback history evaluation time")
    values = dict.fromkeys(FeedbackOutcome, 0.0)
    for record in records:
        for bucket in record_buckets(record):
            start = require_utc_datetime(bucket.get("start_at", ""), "feedback bucket start_at")
            # A bucket represents its complete interval. Using its deterministic
            # end prevents same-bucket samples from falling below the integer
            # floor merely because they arrived after the bucket boundary.
            effective_at = start + timedelta(seconds=policy.get("bucket_seconds", 0))
            age_seconds = max(0.0, (evaluation - effective_at).total_seconds())
            weight = 0.5 ** (age_seconds / policy.get("half_life_seconds", 0.0))
            stats = bucket.get("statistics", {})
            for outcome, field_name in FEEDBACK_OUTCOME_COUNTER_FIELDS.items():
                values[outcome] = values.get(outcome, 0.0) + stats.get(field_name, 0) * weight
    return values


def statement_record_partition(record: dict) -> tuple[str, str]:
    """Return the history partition (statement, policy) of a statement aggregate."""
    key = record.get("key", {})
    result = (key.get("statement_id", ""), key.get("policy_fingerprint", ""))
    return result


def relationship_record_partition(record: dict) -> tuple[str, str, str]:
    """Return the history partition (statement, policy, constraint) of a relationship aggregate."""
    key = record.get("key", {})
    statement = key.get("statement", {})
    result = (statement.get("statement_id", ""), statement.get("policy_fingerprint", ""), key.get("constraint_fingerprint", ""))
    return result


def record_partitions(records: dict, partition) -> dict:
    """Index record fingerprints by history partition, keeping record order."""
    partitions: dict[tuple, dict[str, bool]] = {}
    for fingerprint, record in records.items():
        partitions.setdefault(partition(record), {})[fingerprint] = True
    return partitions


def copied_partitions(partitions: dict) -> dict:
    result = {partition: dict(members) for partition, members in partitions.items()}
    return result


def remove_partition_member(partitions: dict, partition: tuple, fingerprint: str) -> None:
    members = partitions.get(partition, {})
    members.pop(fingerprint, False)
    if not members:
        partitions.pop(partition, {})


class FeedbackStore:
    """Thread-safe authoritative owner for process-memory feedback state.

    History reads use partition indexes kept beside the record maps, so one
    candidate's lookup examines only its own statement and relationship
    partitions instead of every retained aggregate.
    """

    def __init__(self, state: object = ()) -> None:
        if state == ():
            state = feedback_state()
        validated_state = validate_feedback_state(state)
        self.internal_lock = threading_RLock()
        self.install(validated_state)

    def install(self, state: dict) -> None:
        policy = freeze_feedback_record(state.get("policy", {}))
        statement_records = tuple(freeze_feedback_record(record) for record in state.get("statement_records", ()))
        relationship_records = tuple(freeze_feedback_record(record) for record in state.get("relationship_records", ()))
        policy_suppressions = tuple(freeze_feedback_record(value) for value in state.get("policy_suppressions", ()))
        stale_exclusions = tuple(freeze_feedback_record(value) for value in state.get("stale_exclusions", ()))
        self.state_dirty = False
        self.internal_policy = policy
        self.internal_statement_records = {
            statement_feedback_key_fingerprint(record.get("key", {})): record for record in statement_records
        }
        self.internal_relationship_records = {
            relationship_feedback_key_fingerprint(record.get("key", {})): record for record in relationship_records
        }
        self.internal_statement_partitions = record_partitions(self.internal_statement_records, statement_record_partition)
        self.internal_relationship_partitions = record_partitions(self.internal_relationship_records, relationship_record_partition)
        self.internal_policy_suppressions = {
            (value.get("statement_id", ""), value.get("namespace", ""), value.get("policy_fingerprint", "")): value
            for value in policy_suppressions
        }
        self.internal_stale_exclusions = {
            (value.get("statement_id", ""), value.get("generation", 0), value.get("generation_available", False)): value
            for value in stale_exclusions
        }
        self.internal_receipts = MutationReceiptLedger(state=state.get("receipts", ()))
        self.internal_statement_evictions = state.get("statement_evictions", 0)
        self.internal_relationship_evictions = state.get("relationship_evictions", 0)
        self.internal_policy_suppression_evictions = state.get("policy_suppression_evictions", 0)
        self.internal_stale_exclusion_evictions = state.get("stale_exclusion_evictions", 0)
        self.internal_state = {
            "policy": policy,
            "statement_records": statement_records,
            "relationship_records": relationship_records,
            "policy_suppressions": policy_suppressions,
            "stale_exclusions": stale_exclusions,
            "receipts": self.internal_receipts.snapshot(),
            "statement_evictions": self.internal_statement_evictions,
            "relationship_evictions": self.internal_relationship_evictions,
            "policy_suppression_evictions": self.internal_policy_suppression_evictions,
            "stale_exclusion_evictions": self.internal_stale_exclusion_evictions,
        }

    @property
    def policy(self) -> dict:
        result = validate_feedback_policy(self.internal_policy)
        return result

    def clone(self) -> "FeedbackStore":
        """Return an independent off-live store without revalidating its records.

        Records are frozen when they enter and replaced, never changed in
        place, so the clone shares them and copies only the containers.
        """
        with self.internal_lock:
            result = FeedbackStore.__new__(FeedbackStore)
            result.internal_lock = threading_RLock()
            result.state_dirty = self.state_dirty
            result.internal_state = self.internal_state
            result.internal_policy = self.internal_policy
            result.internal_statement_records = dict(self.internal_statement_records)
            result.internal_relationship_records = dict(self.internal_relationship_records)
            result.internal_statement_partitions = copied_partitions(self.internal_statement_partitions)
            result.internal_relationship_partitions = copied_partitions(self.internal_relationship_partitions)
            result.internal_policy_suppressions = dict(self.internal_policy_suppressions)
            result.internal_stale_exclusions = dict(self.internal_stale_exclusions)
            result.internal_receipts = self.internal_receipts.clone()
            result.internal_statement_evictions = self.internal_statement_evictions
            result.internal_relationship_evictions = self.internal_relationship_evictions
            result.internal_policy_suppression_evictions = self.internal_policy_suppression_evictions
            result.internal_stale_exclusion_evictions = self.internal_stale_exclusion_evictions
            return result

    def snapshot(self) -> dict:
        with self.internal_lock:
            if not self.state_dirty:
                result = trusted_feedback_state_copy(self.internal_state)
                return result
            state = {
                "policy": self.internal_policy,
                "statement_records": tuple(record for _, record in sorted(self.internal_statement_records.items())),
                "relationship_records": tuple(record for _, record in sorted(self.internal_relationship_records.items())),
                "policy_suppressions": tuple(sorted(self.internal_policy_suppressions.values(), key=policy_suppression_signature)),
                "stale_exclusions": tuple(sorted(self.internal_stale_exclusions.values(), key=stale_exclusion_signature)),
                "receipts": self.internal_receipts.snapshot(),
                "statement_evictions": self.internal_statement_evictions,
                "relationship_evictions": self.internal_relationship_evictions,
                "policy_suppression_evictions": self.internal_policy_suppression_evictions,
                "stale_exclusion_evictions": self.internal_stale_exclusion_evictions,
            }
            self.internal_state = state
            self.state_dirty = False
            result = trusted_feedback_state_copy(state)
            return result

    def adopt(self, candidate: "FeedbackStore") -> None:
        """Publish an off-live clone of this store without revalidating it."""
        with self.internal_lock, candidate.internal_lock:
            self.internal_state = candidate.internal_state
            self.state_dirty = candidate.state_dirty
            self.internal_policy = candidate.internal_policy
            self.internal_statement_records = dict(candidate.internal_statement_records)
            self.internal_relationship_records = dict(candidate.internal_relationship_records)
            self.internal_statement_partitions = copied_partitions(candidate.internal_statement_partitions)
            self.internal_relationship_partitions = copied_partitions(candidate.internal_relationship_partitions)
            self.internal_policy_suppressions = dict(candidate.internal_policy_suppressions)
            self.internal_stale_exclusions = dict(candidate.internal_stale_exclusions)
            self.internal_receipts = candidate.internal_receipts.clone()
            self.internal_statement_evictions = candidate.internal_statement_evictions
            self.internal_relationship_evictions = candidate.internal_relationship_evictions
            self.internal_policy_suppression_evictions = candidate.internal_policy_suppression_evictions
            self.internal_stale_exclusion_evictions = candidate.internal_stale_exclusion_evictions

    def replace_from_snapshot(self, state: dict) -> None:
        """Validate a complete feedback state and install it as the live state."""
        validated_state = validate_feedback_state(state)
        with self.internal_lock:
            self.install(validated_state)

    def apply_observation(self, observation: dict) -> None:
        """Fold one store-validated observation into the statement, relationship and suppression state.

        Called only by ``internal_plan`` on an off-live clone while the live store's
        ``internal_lock`` is held, so this method takes no lock of its own.
        """
        self.state_dirty = True
        # The observation was validated at the store boundary; its statement and
        # relationship keys are the exact partitions its aggregates live under.
        statement_key = {
            "statement_id": observation.get("statement_id", ""),
            "generation": observation.get("generation", 0),
            "generation_available": observation.get("generation_available", False),
            "policy_fingerprint": observation.get("policy_fingerprint", ""),
        }
        statement_fingerprint = trusted_feedback_key_fingerprint(statement_key)
        statement = self.internal_statement_records.get(statement_fingerprint, {})
        if statement:
            updated_statement = trusted_feedback_record_apply(statement, statement_key, observation, self.internal_policy)
        else:
            updated_statement = trusted_feedback_record_from_observation(statement_key, observation, self.internal_policy)
        frozen_statement = freeze_feedback_record(updated_statement)
        self.internal_statement_records[statement_fingerprint] = frozen_statement
        statement_members = self.internal_statement_partitions.setdefault(statement_record_partition(frozen_statement), {})
        statement_members[statement_fingerprint] = True
        relationship_key = {
            "query_identity": observation.get("query_identity", {}),
            "scope": observation.get("scope", {}),
            "constraint_fingerprint": observation.get("constraint_fingerprint", ""),
            "statement": statement_key,
        }
        relationship_fingerprint = trusted_feedback_key_fingerprint(relationship_key)
        relationship = self.internal_relationship_records.get(relationship_fingerprint, {})
        if relationship:
            updated_relationship = trusted_feedback_record_apply(relationship, relationship_key, observation, self.internal_policy)
        else:
            updated_relationship = trusted_feedback_record_from_observation(relationship_key, observation, self.internal_policy)
        frozen_relationship = freeze_feedback_record(updated_relationship)
        self.internal_relationship_records[relationship_fingerprint] = frozen_relationship
        relationship_members = self.internal_relationship_partitions.setdefault(
            relationship_record_partition(frozen_relationship), {}
        )
        relationship_members[relationship_fingerprint] = True
        outcome = observation.get("outcome", FeedbackOutcome.CANDIDATE)
        suppression_key = (
            observation.get("statement_id", ""),
            observation.get("scope", {}).get("namespace", ""),
            observation.get("policy_fingerprint", ""),
        )
        if outcome == FeedbackOutcome.REJECTED_POLICY:
            self.internal_policy_suppressions[suppression_key] = freeze_feedback_record(
                policy_suppression(*suppression_key, observation.get("observed_at", ""))
            )
        elif outcome == FeedbackOutcome.ACCEPTED:
            self.internal_policy_suppressions.pop(suppression_key, {})
        if outcome == FeedbackOutcome.REJECTED_STALE:
            exclusion = stale_exclusion(
                observation.get("statement_id", ""),
                observation.get("generation", 0),
                observation.get("generation_available", False),
                observation.get("observed_at", ""),
            )
            exclusion_key = (
                exclusion.get("statement_id", ""),
                exclusion.get("generation", 0),
                exclusion.get("generation_available", False),
            )
            self.internal_stale_exclusions[exclusion_key] = freeze_feedback_record(exclusion)

    def enforce_capacity(self) -> None:
        """Evict the least recently observed records until both record sets fit the policy capacities.

        Called only by ``internal_plan`` on an off-live clone while the live store's
        ``internal_lock`` is held, so this method takes no lock of its own.
        """
        statement_capacity = self.internal_policy.get("max_statement_records", 0)
        relationship_capacity = self.internal_policy.get("max_relationship_records", 0)
        while len(self.internal_statement_records) > statement_capacity:
            oldest_key = min(
                self.internal_statement_records,
                key=lambda key: (utc_datetime(self.internal_statement_records.get(key, {}).get("last_observed_at", "")), key),
            )
            remove_partition_member(
                self.internal_statement_partitions,
                statement_record_partition(self.internal_statement_records.get(oldest_key, {})),
                oldest_key,
            )
            del self.internal_statement_records[oldest_key]
            self.internal_statement_evictions += 1
        while len(self.internal_relationship_records) > relationship_capacity:
            oldest_key = min(
                self.internal_relationship_records,
                key=lambda key: (utc_datetime(self.internal_relationship_records.get(key, {}).get("last_observed_at", "")), key),
            )
            remove_partition_member(
                self.internal_relationship_partitions,
                relationship_record_partition(self.internal_relationship_records.get(oldest_key, {})),
                oldest_key,
            )
            del self.internal_relationship_records[oldest_key]
            self.internal_relationship_evictions += 1
        while len(self.internal_policy_suppressions) > statement_capacity:
            oldest_key = min(
                self.internal_policy_suppressions,
                key=lambda key: (utc_datetime(self.internal_policy_suppressions.get(key, {}).get("observed_at", "")), key),
            )
            del self.internal_policy_suppressions[oldest_key]
            self.internal_policy_suppression_evictions += 1
        while len(self.internal_stale_exclusions) > statement_capacity:
            oldest_key = min(
                self.internal_stale_exclusions,
                key=lambda key: (utc_datetime(self.internal_stale_exclusions.get(key, {}).get("observed_at", "")), key),
            )
            del self.internal_stale_exclusions[oldest_key]
            self.internal_stale_exclusion_evictions += 1

    def prepare(
        self,
        request_id: str,
        observations: tuple[dict, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> dict:
        require_text(request_id, "feedback request_id", MAX_REFERENCE_ID_BYTES)
        if not isinstance(observations, tuple) or not observations:
            raise InvalidRequestError("feedback observations must be a non-empty tuple of FeedbackObservation values")
        if len(observations) > MAX_FEEDBACK_OBSERVATIONS:
            raise InvalidRequestError(f"feedback observations exceed the limit of {MAX_FEEDBACK_OBSERVATIONS}")
        validated_observations = tuple(validate_feedback_observation(observation) for observation in observations)
        result = self.prepare_validated(request_id, validated_observations, lifecycle_status)
        return result

    def prepare_validated(
        self,
        request_id: str,
        validated_observations: tuple[dict, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> dict:
        """Prepare observations already validated by an internal service boundary."""
        with self.internal_lock:
            before = self.snapshot()
            planned = self.internal_plan(request_id, validated_observations, lifecycle_status)
            receipt = validate_mutation_receipt(planned.get("receipt", {}))
            if planned.get("replayed", False):
                # A replay changes nothing, so its isolated after state is a copy of before.
                result = {"before": before, "after": trusted_feedback_state_copy(before), "receipt": receipt, "replayed": True}
                return result
            after = planned.get("candidate", self).snapshot()
            result = {"before": before, "after": after, "receipt": receipt, "replayed": False}
            return result

    def apply_validated(
        self,
        request_id: str,
        validated_observations: tuple[dict, ...],
        lifecycle_status: LifecycleHandoffStatus = LifecycleHandoffStatus.NOT_APPLICABLE,
    ) -> dict:
        """Record and publish validated observations, returning the receipt and replay flag.

        This is ``prepare_validated`` followed by publication, without the
        before and after state projections that only the prepare contract needs.
        """
        with self.internal_lock:
            planned = self.internal_plan(request_id, validated_observations, lifecycle_status)
            replayed = planned.get("replayed", False)
            if not replayed:
                self.adopt(planned.get("candidate", self))
            result = {"receipt": planned.get("receipt", {}), "replayed": replayed}
            return result

    def internal_plan(
        self,
        request_id: str,
        validated_observations: tuple[dict, ...],
        lifecycle_status: LifecycleHandoffStatus,
    ) -> dict:
        """Check the receipt and apply observations to an off-live clone.

        A replay changes nothing, so its candidate is this live store itself;
        callers publish a candidate only when ``replayed`` is false.
        """
        require_text(request_id, "feedback request_id", MAX_REFERENCE_ID_BYTES)
        if not validated_observations or len(validated_observations) > MAX_FEEDBACK_OBSERVATIONS:
            raise InvalidRequestError("validated feedback observations must be a non-empty bounded tuple")
        if not isinstance(lifecycle_status, LifecycleHandoffStatus):
            raise InvalidRequestError("feedback lifecycle_status must be LifecycleHandoffStatus")
        signature = feedback_payload_signature(validated_observations)
        with self.internal_lock:
            lookup = self.internal_receipts.lookup(request_id, MutationOperation.RECORD_FEEDBACK, signature)
            # The ledger always reports an outcome; a missing one must not authorize a mutation.
            lookup_outcome = lookup.get("outcome", ReceiptLookupOutcome.CONFLICT)
            if lookup_outcome == ReceiptLookupOutcome.REPLAY:
                result: dict = {"candidate": self, "receipt": receipt_lookup_receipt(lookup), "replayed": True}
                return result
            if lookup_outcome == ReceiptLookupOutcome.CONFLICT:
                raise ConflictError(f"feedback request_id is associated with a different observation: {request_id}")
            if lookup_outcome == ReceiptLookupOutcome.IN_PROGRESS:
                raise ConflictError(f"feedback request is already in progress: {request_id}")
            if lookup_outcome == ReceiptLookupOutcome.EXPIRED:
                raise ConflictError(f"feedback request result expired and cannot be reapplied safely: {request_id}")
            candidate = self.clone()
            for observation in validated_observations:
                candidate.apply_observation(observation)
            candidate.enforce_capacity()
            outcomes = {outcome.value: 0 for outcome in FeedbackOutcome}
            for observation in validated_observations:
                outcome_value = observation.get("outcome", FeedbackOutcome.CANDIDATE).value
                outcomes[outcome_value] = outcomes.get(outcome_value, 0) + 1
            created_at = max((observation.get("observed_at", "") for observation in validated_observations), key=utc_datetime)
            receipt = mutation_receipt(
                sequence=candidate.internal_receipts.next_sequence,
                request_id=request_id,
                operation=MutationOperation.RECORD_FEEDBACK,
                payload_signature=signature,
                result_code=MutationResultCode.FEEDBACK_RECORDED,
                affected_generations=(),
                result={
                    "observation_count": len(validated_observations),
                    "outcomes": outcomes,
                    "lifecycle_status": lifecycle_status.value,
                },
                completion_state=ReceiptCompletionState.COMPLETED,
                created_at=created_at,
            )
            candidate.internal_receipts.record(receipt)
            result = {"candidate": candidate, "receipt": receipt, "replayed": False}
            return result

    def history(
        self,
        query_identity: dict,
        constraint: str,
        statement_id: str,
        current_generation: int,
        policy_fingerprint: str,
        evaluation_time: str,
    ) -> dict:
        """Return the aged feedback posterior for one statement under one query and constraint.

        Statement records count with any generation up to ``current_generation``;
        relationship records also need the same query identity, scope and constraint.
        Outcomes are aged to ``evaluation_time`` by the policy half-life. Statement rejects
        count only quality and stale outcomes; relationship rejects also count context and
        policy outcomes. Each side is available once its accepts plus rejects reach
        ``minimum_verdict_samples``, and its value is the posterior
        ``(accepts + prior_accept) / (accepts + rejects + prior_accept + prior_reject)``.
        ``value`` averages the available sides, or is ``0.0`` with ``available`` false
        when neither is.
        """
        try:
            validated_query_identity = validate_query_identity(query_identity)
        except IdentityValidationError as err:
            raise InvalidRequestError("feedback history query_identity must be QueryIdentity") from err
        internal_fingerprint(constraint, "feedback history constraint_fingerprint")
        require_text(statement_id, "feedback history statement_id", MAX_STATEMENT_ID_BYTES)
        internal_integer(current_generation, "feedback history current_generation", 1)
        policy_value = internal_fingerprint(policy_fingerprint, "feedback history policy_fingerprint")
        require_utc_datetime(evaluation_time, "feedback history evaluation_time")
        with self.internal_lock:
            statement_partition = self.internal_statement_partitions.get((statement_id, policy_value), {})
            statement_records = tuple(
                record
                for record in (self.internal_statement_records.get(fingerprint, {}) for fingerprint in statement_partition)
                if record.get("key", {}).get("generation_available", False)
                and record.get("key", {}).get("generation", 0) <= current_generation
            )
            relationship_partition = self.internal_relationship_partitions.get((statement_id, policy_value, constraint), {})
            relationship_records = tuple(
                record
                for record in (self.internal_relationship_records.get(fingerprint, {}) for fingerprint in relationship_partition)
                if record.get("key", {}).get("statement", {}).get("generation_available", False)
                and record.get("key", {}).get("statement", {}).get("generation", 0) <= current_generation
                and record.get("key", {}).get("query_identity", {}) == validated_query_identity
                and record.get("key", {}).get("scope", {}) == validated_query_identity.get("scope", {})
            )
            statement_values = aged_values(statement_records, evaluation_time, self.internal_policy)
            relationship_values = aged_values(relationship_records, evaluation_time, self.internal_policy)

        statement_accept = statement_values.get(FeedbackOutcome.ACCEPTED, 0.0)
        statement_quality_reject = statement_values.get(FeedbackOutcome.REJECTED_QUALITY, 0.0)
        statement_reject = statement_quality_reject + statement_values.get(FeedbackOutcome.REJECTED_STALE, 0.0)
        relationship_accept = relationship_values.get(FeedbackOutcome.ACCEPTED, 0.0)
        relationship_reject = sum(
            relationship_values.get(outcome, 0.0)
            for outcome in (
                FeedbackOutcome.REJECTED_QUALITY,
                FeedbackOutcome.REJECTED_CONTEXT,
                FeedbackOutcome.REJECTED_STALE,
                FeedbackOutcome.REJECTED_POLICY,
            )
        )
        statement_samples = statement_accept + statement_reject
        relationship_samples = relationship_accept + relationship_reject
        minimum_samples = self.internal_policy.get("minimum_verdict_samples", 0)
        statement_available = statement_samples >= minimum_samples
        relationship_available = relationship_samples >= minimum_samples
        prior_accept = self.internal_policy.get("prior_accept", 0.0)
        prior_reject = self.internal_policy.get("prior_reject", 0.0)

        def posterior(accepted: float, rejected: float) -> float:
            numerator = accepted + prior_accept
            denominator = accepted + rejected + prior_accept + prior_reject
            result = numerator / denominator
            return result

        statement_value = posterior(statement_accept, statement_reject) if statement_available else 0.0
        relationship_value = posterior(relationship_accept, relationship_reject) if relationship_available else 0.0
        available = statement_available or relationship_available
        if statement_available and relationship_available:
            value = (statement_value + relationship_value) / 2.0
        elif statement_available:
            value = statement_value
        elif relationship_available:
            value = relationship_value
        else:
            value = 0.0
        result = feedback_history(
            value=value,
            available=available,
            statement_value=statement_value,
            statement_available=statement_available,
            relationship_value=relationship_value,
            relationship_available=relationship_available,
            statement_samples=statement_samples,
            relationship_samples=relationship_samples,
            policy_fingerprint=policy_value,
            feedback_policy_value=feedback_policy_fingerprint(self.internal_policy),
        )
        return result

    def stale_excluded(self, statement_id: str, current_generation: int, generation_available: bool = True) -> bool:
        require_text(statement_id, "stale exclusion statement_id", MAX_STATEMENT_ID_BYTES)
        internal_integer(current_generation, "stale exclusion current_generation", 0)
        require_bool(generation_available, "stale exclusion generation_available")
        result = self.trusted_stale_excluded(statement_id, current_generation, generation_available)
        return result

    def trusted_stale_excluded(
        self,
        statement_id: str,
        current_generation: int,
        generation_available: bool = True,
    ) -> bool:
        """Check a validated in-process eligibility tuple."""
        result = False
        with self.internal_lock:
            for exclusion in self.internal_stale_exclusions.values():
                if exclusion.get("statement_id", "") != statement_id:
                    continue
                if not exclusion.get("generation_available", False):
                    result = not generation_available
                    break
                if generation_available and current_generation >= exclusion.get("generation", 0):
                    result = True
                    break
        return result

    def policy_suppressed(self, statement_id: str, namespace: str, policy_fingerprint: str) -> bool:
        require_text(statement_id, "policy suppression statement_id", MAX_STATEMENT_ID_BYTES)
        require_text(namespace, "policy suppression namespace", MAX_NAMESPACE_BYTES, allow_empty=True)
        policy_value = internal_fingerprint(policy_fingerprint, "policy suppression policy_fingerprint")
        result = self.trusted_policy_suppressed(statement_id, namespace, policy_value)
        return result

    def trusted_policy_suppressed(self, statement_id: str, namespace: str, policy_fingerprint: str) -> bool:
        """Check a validated in-process policy-suppression tuple."""
        with self.internal_lock:
            result = (statement_id, namespace, policy_fingerprint) in self.internal_policy_suppressions
        return result

    def inspect(self, limit: int = MAX_INSPECTION_RECORDS) -> dict:
        internal_integer(limit, "feedback inspection limit", 1, MAX_INSPECTION_RECORDS)
        with self.internal_lock:
            statement_values = sorted(
                self.internal_statement_records.values(),
                key=lambda record: statement_feedback_key_fingerprint(record.get("key", {})),
            )
            relationship_values = sorted(
                self.internal_relationship_records.values(),
                key=lambda record: relationship_feedback_key_fingerprint(record.get("key", {})),
            )
            receipt_snapshot = self.internal_receipts.snapshot()
            receipt_values = sorted(
                receipt_snapshot.get("receipts", []),
                key=lambda value: value.get("sequence", 0),
                reverse=True,
            )
            receipt_tombstones = receipt_snapshot.get("tombstones", [])
            result = {
                "policy_fingerprint": feedback_policy_fingerprint(self.internal_policy),
                "statement_record_count": len(statement_values),
                "relationship_record_count": len(relationship_values),
                "policy_suppression_count": len(self.internal_policy_suppressions),
                "stale_exclusion_count": len(self.internal_stale_exclusions),
                "receipt_count": len(receipt_values),
                "receipt_tombstone_count": len(receipt_tombstones),
                "receipts": [
                    {
                        "diagnostic_id": internal_diagnostic_id(value.get("request_id", "")),
                        "sequence": value.get("sequence", 0),
                        "result_code": value.get("result_code", ""),
                        "completion_state": value.get("completion_state", ""),
                        "observation_count": value.get("result", {}).get("observation_count", 0),
                        "outcomes": value.get("result", {}).get("outcomes", {}),
                        "lifecycle_status": value.get("result", {}).get("lifecycle_status", ""),
                    }
                    for value in receipt_values[:limit]
                ],
                "omitted_receipt_count": max(0, len(receipt_values) - limit),
                "statement_evictions": self.internal_statement_evictions,
                "relationship_evictions": self.internal_relationship_evictions,
                "policy_suppression_evictions": self.internal_policy_suppression_evictions,
                "stale_exclusion_evictions": self.internal_stale_exclusion_evictions,
                "statements": [
                    {
                        "diagnostic_id": internal_diagnostic_id(statement_feedback_key_fingerprint(record.get("key", {}))),
                        "statistics": feedback_statistics_to_dict(record.get("raw", {})),
                        "bucket_count": len(record.get("buckets", ())),
                        "last_outcome": record.get("last_outcome", FeedbackOutcome.CANDIDATE).value,
                        "last_observed_at": record.get("last_observed_at", ""),
                    }
                    for record in statement_values[:limit]
                ],
                "omitted_statement_count": max(0, len(statement_values) - limit),
                "relationships": [
                    {
                        "diagnostic_id": internal_diagnostic_id(relationship_feedback_key_fingerprint(record.get("key", {}))),
                        "statistics": feedback_statistics_to_dict(record.get("raw", {})),
                        "bucket_count": len(record.get("buckets", ())),
                        "last_outcome": record.get("last_outcome", FeedbackOutcome.CANDIDATE).value,
                        "last_observed_at": record.get("last_observed_at", ""),
                    }
                    for record in relationship_values[:limit]
                ],
                "omitted_relationship_count": max(0, len(relationship_values) - limit),
            }
        return result
