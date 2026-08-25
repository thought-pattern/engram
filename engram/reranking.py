"""Bounded transparent reranking for a small fused-candidate shortlist."""

import json
import math
import threading
import time
from collections.abc import Mapping
from types import MappingProxyType

from engram.config import reranker_config
from engram.errors import InvalidRequestError, ResolutionCancelledError

RERANKER_CONTRACT_VERSION = 1
RERANKER_IMPLEMENTATION = "transparent_logistic_v1"
RERANKER_FEATURES = (
    "base_score",
    "exact",
    "pattern",
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
RERANKER_COEFFICIENTS: Mapping[str, float] = MappingProxyType(
    {
        "base_score": 2.40,
        "exact": 4.00,
        "pattern": 0.25,
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
)
RERANKER_INTERCEPT = -1.20


def _unit(value: object, name: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
        raise InvalidRequestError(f"reranker {name} must be a finite number")
    return max(0.0, min(1.0, float(value)))


def _score(features: Mapping[str, object]) -> tuple[float, dict[str, float]]:
    normalized = {name: _unit(features.get(name, 0.0), name) for name in RERANKER_FEATURES}
    logit = RERANKER_INTERCEPT + sum(RERANKER_COEFFICIENTS[name] * normalized[name] for name in RERANKER_FEATURES)
    if logit >= 0:
        exponent = math.exp(-logit)
        score = 1.0 / (1.0 + exponent)
    else:
        exponent = math.exp(logit)
        score = exponent / (1.0 + exponent)
    return score, normalized


class TransparentLogisticReranker:
    """A fixed, inspectable scorer with deterministic baseline fallback."""

    def __init__(self, settings: Mapping[str, object], clock_ns: object = ()) -> None:
        try:
            self.settings = reranker_config(**dict(settings))
        except (TypeError, ValueError) as error:
            raise InvalidRequestError(str(error)) from error
        if clock_ns != () and not callable(clock_ns):
            raise InvalidRequestError("reranker clock_ns must be callable")
        self._clock_ns = clock_ns if callable(clock_ns) else time.monotonic_ns
        self._lock = threading.Lock()
        self._requests = 0
        self._completed = 0
        self._fallbacks = 0
        self._cancellations = 0
        self._last_reason = "disabled" if not self.settings["enabled"] else "ready"

    @property
    def enabled(self) -> bool:
        return self.settings["enabled"]

    def health(self) -> dict:
        with self._lock:
            return {
                "enabled": self.enabled,
                "ready": self.enabled,
                "implementation": self.settings["implementation"],
                "model_version": self.settings["model_version"],
                "contract_version": RERANKER_CONTRACT_VERSION,
                "requests": self._requests,
                "completed": self._completed,
                "fallbacks": self._fallbacks,
                "cancellations": self._cancellations,
                "last_reason": self._last_reason,
            }

    def _record(self, reason: str, *, completed: bool = False, fallback: bool = False, cancelled: bool = False) -> None:
        with self._lock:
            self._requests += 1
            self._completed += int(completed)
            self._fallbacks += int(fallback)
            self._cancellations += int(cancelled)
            self._last_reason = reason

    def record_fallback(self, reason: str) -> None:
        """Record a fallback isolated by the owning fusion engine."""
        self._record(reason, fallback=True)

    def rerank(self, shortlist: tuple[Mapping[str, object], ...], cooperative_check: object = ()) -> dict:
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
                key=lambda value: (-_unit(value["base_score"], "base_score"), str(value["statement_id"])),
            )
        )
        for value in baseline:
            if not isinstance(value["statement_id"], str) or not value["statement_id"]:
                raise InvalidRequestError("reranker statement_id must be a non-empty string")
            if not isinstance(value["features"], Mapping):
                raise InvalidRequestError("reranker features must be an object")
        started = self._clock_ns()
        if not self.enabled:
            return {
                "applied": False,
                "reason": "disabled",
                "model_version": self.settings["model_version"],
                "elapsed_ns": 0,
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
        if len(baseline) > self.settings["shortlist_size"]:
            self._record("shortlist_budget", fallback=True)
            return {
                "applied": False,
                "reason": "shortlist_budget",
                "model_version": self.settings["model_version"],
                "elapsed_ns": max(0, self._clock_ns() - started),
                "model_time_target_exceeded": False,
                "input_bytes": 0,
                "scores": [],
            }
        serialized = json.dumps(baseline, sort_keys=True, separators=(",", ":"), default=str).encode()
        if len(serialized) > self.settings["max_input_bytes"]:
            self._record("input_budget", fallback=True)
            elapsed = max(0, self._clock_ns() - started)
            return {
                "applied": False,
                "reason": "input_budget",
                "model_version": self.settings["model_version"],
                "elapsed_ns": elapsed,
                "model_time_target_exceeded": elapsed > self.settings["max_model_time_ms"] * 1_000_000,
                "input_bytes": len(serialized),
                "scores": [],
            }
        try:
            if callable(cooperative_check):
                cooperative_check()
            scored = []
            for value in baseline:
                if callable(cooperative_check):
                    cooperative_check()
                score, normalized = _score(value["features"])
                scored.append(
                    {
                        "statement_id": value["statement_id"],
                        "base_score": _unit(value["base_score"], "base_score"),
                        "score": score,
                        "features": normalized,
                    }
                )
        except ResolutionCancelledError:
            self._record("cancelled", cancelled=True)
            raise
        elapsed = max(0, self._clock_ns() - started)
        scored.sort(key=lambda value: (-value["score"], -value["base_score"], value["statement_id"]))
        self._record("completed", completed=True)
        return {
            "applied": True,
            "reason": "completed",
            "model_version": self.settings["model_version"],
            "elapsed_ns": elapsed,
            "model_time_target_exceeded": elapsed > self.settings["max_model_time_ms"] * 1_000_000,
            "input_bytes": len(serialized),
            "scores": scored,
        }
