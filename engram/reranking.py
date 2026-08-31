"""Bounded transparent reranking for a small fused-candidate shortlist."""

from collections.abc import Mapping
from json import dumps as json_dumps
from math import exp as math_exp, isfinite as math_isfinite
from threading import Lock as threading_Lock
from time import monotonic_ns as time_monotonic_ns

from engram.config import reranker_config
from engram.errors import InvalidRequestError, ResolutionCancelledError

RERANKER_CONTRACT_VERSION = 1
RERANKER_IMPLEMENTATION = "transparent_logistic_v1"
RERANKER_FEATURES = (
    "base_score",
    "exact",
    "lexical",
    "semantic",
    "entity",
    "relation",
    "object_type",
    "support",
    "freshness",
    "authority",
    "agreement",
)
RERANKER_COEFFICIENTS: dict[str, float] = {
        "base_score": 2.40,
        "exact": 4.00,
        "lexical": 0.65,
        "semantic": 0.85,
        "entity": 0.55,
        "relation": 0.55,
        "object_type": 0.40,
        "support": 0.80,
        "freshness": 0.20,
        "authority": 0.50,
        "agreement": 0.65,
    }
RERANKER_INTERCEPT = -1.20


def unit(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math_isfinite(float(value)):
        raise InvalidRequestError(f"reranker {name} must be a finite number")
    result = max(0.0, min(1.0, float(value)))
    return result


def internal_score(features: dict[str, object]) -> tuple[float, dict[str, float]]:
    normalized = {name: unit(features.get(name, 0.0), name) for name in RERANKER_FEATURES}
    logit = RERANKER_INTERCEPT + sum(RERANKER_COEFFICIENTS.get(name, 0.0) * normalized[name] for name in RERANKER_FEATURES)
    if logit >= 0:
        exponent = math_exp(-logit)
        score = 1.0 / (1.0 + exponent)
    else:
        exponent = math_exp(logit)
        score = exponent / (1.0 + exponent)
    return score, normalized


class TransparentLogisticReranker:
    """A fixed, inspectable scorer with deterministic baseline fallback."""

    def __init__(self, settings: dict[str, object], clock_ns: object = ()) -> None:
        try:
            self.settings = reranker_config(**dict(settings))
        except (TypeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        if clock_ns != () and not callable(clock_ns):
            raise InvalidRequestError("reranker clock_ns must be callable")
        self.internal_clock_ns = clock_ns if callable(clock_ns) else time_monotonic_ns
        self.internal_lock = threading_Lock()
        self.internal_requests = 0
        self.internal_completed = 0
        self.internal_fallbacks = 0
        self.internal_cancellations = 0
        self.internal_last_reason = "disabled" if not self.settings["enabled"] else "ready"

    @property
    def enabled(self) -> bool:
        result = self.settings["enabled"]
        return result

    def health(self) -> dict:
        with self.internal_lock:
            result = {
                "enabled": self.enabled,
                "ready": self.enabled,
                "implementation": self.settings["implementation"],
                "model_version": self.settings["model_version"],
                "contract_version": RERANKER_CONTRACT_VERSION,
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

    def rerank(self, shortlist: tuple[dict[str, object], ...], cooperative_check: object = ()) -> dict:
        """Score one bounded shortlist or preserve the complete baseline order."""
        if not isinstance(shortlist, tuple):
            raise InvalidRequestError("reranker shortlist must be a tuple")
        if cooperative_check != () and not callable(cooperative_check):
            raise InvalidRequestError("reranker cooperative_check must be callable")
        baseline = tuple(
            sorted(
                (
                    {
                        "statement_id": value.get("statement_id"),
                        "base_score": value.get("base_score"),
                        "features": value.get("features"),
                    }
                    for value in shortlist
                ),
                key=lambda value: (-unit(value["base_score"], "base_score"), str(value["statement_id"])),
            )
        )
        for value in baseline:
            if not isinstance(value["statement_id"], str) or not value["statement_id"]:
                raise InvalidRequestError("reranker statement_id must be a non-empty string")
            if not isinstance(value["features"], Mapping):
                raise InvalidRequestError("reranker features must be an object")
        started = self.internal_clock_ns()
        if not self.enabled:
            result = {
                "applied": False,
                "reason": "disabled",
                "model_version": self.settings["model_version"],
                "elapsed_ns": 0,
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
            return result
        if len(baseline) > self.settings["shortlist_size"]:
            self.internal_record("shortlist_budget", fallback=True)
            result = {
                "applied": False,
                "reason": "shortlist_budget",
                "model_version": self.settings["model_version"],
                "elapsed_ns": max(0, self.internal_clock_ns() - started),
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
            return result
        serialized = json_dumps(baseline, sort_keys=True, separators=(",", ":"), default=str).encode()
        if len(serialized) > self.settings["max_input_bytes"]:
            self.internal_record("input_budget", fallback=True)
            elapsed = max(0, self.internal_clock_ns() - started)
            result = {
                "applied": False,
                "reason": "input_budget",
                "model_version": self.settings["model_version"],
                "elapsed_ns": elapsed,
                "model_time_target_exceeded": elapsed > self.settings["max_model_time_ms"] * 1_000_000,
                "input_bytes": len(serialized),
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
                score, normalized = internal_score(value["features"])
                scored.append(
                    {
                        "statement_id": value["statement_id"],
                        "base_score": unit(value["base_score"], "base_score"),
                        "score": score,
                        "features": normalized,
                    }
                )
        except ResolutionCancelledError:
            self.internal_record("cancelled", cancelled=True)
            raise
        elapsed = max(0, self.internal_clock_ns() - started)
        scored.sort(key=lambda value: (-value["score"], -value["base_score"], value["statement_id"]))
        self.internal_record("completed", completed=True)
        result = {
            "applied": True,
            "reason": "completed",
            "model_version": self.settings["model_version"],
            "elapsed_ns": elapsed,
            "model_time_target_exceeded": elapsed > self.settings["max_model_time_ms"] * 1_000_000,
            "input_bytes": len(serialized),
            "scores": scored,
        }
        return result
