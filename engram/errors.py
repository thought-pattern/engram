"""Transport-neutral application errors exposed by :mod:`engram.service`.

This is the lowest Engram module: it imports no other Engram module, so every
layer can raise these errors and describe a failure for its logs without a cycle.
"""

from pathlib import Path
from traceback import extract_tb as traceback_extract_tb


def failure_origin(error: BaseException) -> str:
    """Describe a failure for a log by its class and innermost frame, never its message.

    Exception messages from graph drivers, model loaders and third-party libraries can
    carry query text, graph values or local paths, so logs name only where the failure
    was raised.
    """
    frames = traceback_extract_tb(error.__traceback__)
    if frames:
        innermost = frames[-1]
        origin = f"{type(error).__name__} at {Path(innermost.filename).name}:{innermost.lineno} in {innermost.name}"
    else:
        origin = type(error).__name__
    return origin


class EngramCoreError(Exception):
    """Base class for stable application-facade failures."""


class InvalidRequestError(ValueError, EngramCoreError):
    """The request is invalid regardless of current core state."""


class IdentityValidationError(InvalidRequestError):
    """An identity contract is malformed or internally inconsistent."""


class ResourceNotFoundError(ValueError, EngramCoreError):
    """A requested conversation, proposal, or statement does not exist."""


class ConflictError(ValueError, EngramCoreError):
    """The request conflicts with an existing lifecycle or idempotency record."""


class ConversationOwnershipError(ValueError, EngramCoreError):
    """A conversation operation did not present the token its owner received at start."""


class LifecycleError(ValueError, EngramCoreError):
    """The core is not in a state that permits the requested operation."""


class ResourceExhaustedError(EngramCoreError):
    """A configured capacity limit refuses the request."""


class VectorSupportScanExceededError(ResourceExhaustedError):
    """Response support fan-out from vector hits exceeds the configured scan limit."""


class WorkingMemoryExceededError(ResourceExhaustedError):
    """A request's working-memory budget refuses further retained work.

    This is a budget refusal, distinct from the interpreter's own ``MemoryError``.
    """


class ResolutionCancelledError(EngramCoreError):
    """A caller cancelled transport-neutral resolution cooperatively."""
