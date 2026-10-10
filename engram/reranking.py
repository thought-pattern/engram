"""Bounded transparent reranking for a small fused-candidate shortlist."""

from math import exp as math_exp, isfinite as math_isfinite
from threading import Lock as threading_Lock
from time import monotonic_ns as time_monotonic_ns

from engram.config import reranker_config
from engram.constants import RERANKER_COEFFICIENTS, RERANKER_FEATURES, RERANKER_INTERCEPT
from engram.errors import InvalidRequestError, ResolutionCancelledError


def unit(value: object, name: str) -> float:
    """Return a reranker feature value that is a finite number from 0.0 to 1.0, or raise."""
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math_isfinite(float(value)):
        raise InvalidRequestError(f"reranker {name} must be a finite number")
    if not 0.0 <= value <= 1.0:
        raise InvalidRequestError(f"reranker {name} must be between 0 and 1")
    result = float(value)
    return result


def internal_score(features: dict) -> tuple[float, dict[str, float]]:
    """Score one candidate's features with the fixed logistic model.

    Every key must be one of RERANKER_FEATURES; a feature that is absent counts
    as 0.0. Returns the probability and the validated feature values.
    """
    unknown = sorted(str(name) for name in set(features) - set(RERANKER_FEATURES))
    if unknown:
        raise InvalidRequestError(f"reranker features are not part of the model: {unknown}")
    normalized = {str(name): unit(features.get(name, 0.0), str(name)) for name in RERANKER_FEATURES}
    logit = RERANKER_INTERCEPT + sum(RERANKER_COEFFICIENTS.get(name, 0.0) * normalized.get(name, 0.0) for name in RERANKER_FEATURES)
    if logit >= 0:
        exponent = math_exp(-logit)
        score = 1.0 / (1.0 + exponent)
    else:
        exponent = math_exp(logit)
        score = exponent / (1.0 + exponent)
    return score, normalized


class TransparentLogisticReranker:
    """A fixed, inspectable scorer with deterministic baseline fallback."""

    def __init__(self, settings: dict, clock_ns: object = ()) -> None:
        try:
            self.settings = reranker_config(**dict(settings))
        except (TypeError, ValueError) as err:
            raise InvalidRequestError(str(err)) from err
        if clock_ns != () and not callable(clock_ns):
            raise InvalidRequestError("reranker clock_ns must be callable")
        self.internal_clock_ns = clock_ns if callable(clock_ns) else time_monotonic_ns
        self.internal_lock = threading_Lock()
        self.internal_requests = 0
        self.internal_completed = 0
        self.internal_fallbacks = 0
        self.internal_cancellations = 0
        self.internal_last_reason = "disabled" if not self.settings.get("enabled", False) else "ready"

    @property
    def enabled(self) -> bool:
        result = self.settings.get("enabled", False)
        return result

    def health(self) -> dict:
        with self.internal_lock:
            result = {
                "enabled": self.enabled,
                "ready": self.enabled,
                "implementation": self.settings.get("implementation", ""),
                "requests": self.internal_requests,
                "completed": self.internal_completed,
                "fallbacks": self.internal_fallbacks,
                "cancellations": self.internal_cancellations,
                "last_reason": self.internal_last_reason,
            }
            return result

    def internal_record(self, reason: str, *, completed: bool = False, fallback: bool = False, cancelled: bool = False) -> None:
        with self.internal_lock:
            self.internal_requests += 1
            self.internal_completed += int(completed)
            self.internal_fallbacks += int(fallback)
            self.internal_cancellations += int(cancelled)
            self.internal_last_reason = reason

    def record_fallback(self, reason: str) -> None:
        """Record a fallback isolated by the owning fusion engine."""
        self.internal_record(reason, fallback=True)

    def clock_ns(self) -> int:
        """Read and validate the injected monotonic clock boundary."""
        value = self.internal_clock_ns()
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise InvalidRequestError("reranker clock must return a non-negative integer")
        return value

    def rerank(self, shortlist: tuple[dict, ...], cooperative_check: object = ()) -> dict:
        """Score one bounded shortlist or preserve the complete baseline order."""
        if not isinstance(shortlist, tuple):
            raise InvalidRequestError("reranker shortlist must be a tuple")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("reranker cooperative_check must be callable")
        if self.enabled and len(shortlist) > self.settings.get("shortlist_size", 0):
            # The cap is known from the tuple length, so an oversized shortlist is
            # refused before any candidate is copied, validated, or sorted.
            started = self.clock_ns()
            self.internal_record("shortlist_budget", fallback=True)
            result = {
                "applied": False,
                "reason": "shortlist_budget",
                "elapsed_ns": max(0, self.clock_ns() - started),
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
            return result
        baseline = tuple(
            sorted(
                (
                    {
                        "statement_id": value.get("statement_id", ""),
                        "base_score": value.get("base_score", 0.0),
                        "features": value.get("features", {}),
                    }
                    for value in shortlist
                ),
                key=lambda value: (
                    -unit(value.get("base_score", 0.0), "base_score"),
                    str(value.get("statement_id", "")),
                ),
            )
        )
        for value in baseline:
            if not isinstance(value.get("statement_id", ""), str) or not value.get("statement_id", ""):
                raise InvalidRequestError("reranker statement_id must be a non-empty string")
            if not isinstance(value.get("features", {}), dict):
                raise InvalidRequestError("reranker features must be an object")
        started = self.clock_ns()
        if not self.enabled:
            result = {
                "applied": False,
                "reason": "disabled",
                "elapsed_ns": 0,
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
            return result
        input_bytes = sum(
            len(value.get("statement_id", "").encode("utf-8"))
            + 8
            + sum(len(name.encode("utf-8")) + 8 for name in RERANKER_FEATURES)
            for value in baseline
        )
        if input_bytes > self.settings.get("max_input_bytes", 0):
            self.internal_record("input_budget", fallback=True)
            elapsed = max(0, self.clock_ns() - started)
            result = {
                "applied": False,
                "reason": "input_budget",
                "elapsed_ns": elapsed,
                "model_time_target_exceeded": elapsed > self.settings.get("max_model_time_ms", 0) * 1_000_000,
                "input_bytes": input_bytes,
                "scores": [],
            }
            return result
        try:
            if callable(cooperative_check):
                cooperative_check()
            scored = []
            for value in baseline:
                if callable(cooperative_check):
                    cooperative_check()
                features = value.get("features", {})
                if not isinstance(features, dict):
                    raise InvalidRequestError("reranker features must be an object")
                score, normalized = internal_score(features)
                scored.append(
                    {
                        "statement_id": value.get("statement_id", ""),
                        "base_score": unit(value.get("base_score", 0.0), "base_score"),
                        "score": score,
                        "features": normalized,
                    }
                )
        except ResolutionCancelledError:
            self.internal_record("cancelled", cancelled=True)
            raise
        elapsed = max(0, self.clock_ns() - started)
        scored.sort(
            key=lambda value: (
                -value.get("score", 0.0),
                -value.get("base_score", 0.0),
                value.get("statement_id", ""),
            )
        )
        self.internal_record("completed", completed=True)
        result = {
            "applied": True,
            "reason": "completed",
            "elapsed_ns": elapsed,
            "model_time_target_exceeded": elapsed > self.settings.get("max_model_time_ms", 0) * 1_000_000,
            "input_bytes": input_bytes,
            "scores": scored,
        }
        return result
