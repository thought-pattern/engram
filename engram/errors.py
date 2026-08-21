"""Transport-neutral application errors exposed by :mod:`engram.service`."""


class EngramCoreError(Exception):
    """Base class for stable application-facade failures."""


class InvalidRequestError(ValueError, EngramCoreError):
    """The request is invalid regardless of current core state."""


class IdentityValidationError(InvalidRequestError):
    """An identity contract is malformed or internally inconsistent."""


class UnsupportedIdentityVersionError(IdentityValidationError):
    """An identity contract uses a schema or normalization version not supported here."""


class ResourceNotFoundError(ValueError, EngramCoreError):
    """A requested conversation, proposal, or statement does not exist."""


class ConflictError(ValueError, EngramCoreError):
    """The request conflicts with an existing lifecycle or idempotency record."""


class LifecycleError(ValueError, EngramCoreError):
    """The core is not in a state that permits the requested operation."""


class ResolutionCancelledError(EngramCoreError):
    """A caller cancelled transport-neutral resolution cooperatively."""


class RewriteLimitError(EngramCoreError):
    """A retrieval rewrite bound stopped processing before a fixed point."""


class PersistenceError(EngramCoreError):
    """A store checkpoint failed after an optional in-memory mutation.

    ``state_changed`` tells an adapter whether the requested mutation was
    already committed to the live single-instance core before persistence
    failed. A later successful ``flush()`` can recover durability.
    """

    def __init__(self, operation: str, cause: Exception, *, state_changed: bool) -> None:
        self.operation = operation
        self.cause = cause
        self.state_changed = state_changed
        super().__init__(f"{operation} failed: {cause}")
