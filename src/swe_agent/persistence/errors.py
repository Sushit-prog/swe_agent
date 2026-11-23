"""Typed errors for the persistence layer."""

from __future__ import annotations


class PersistenceError(Exception):
    """Base class for persistence failures."""


class SchemaVersionError(PersistenceError):
    """Database schema version is incompatible with this build."""


class RunAlreadyExists(PersistenceError):
    """A run with the given id already exists."""


class RunNotFound(PersistenceError):
    """No run with the given id exists."""


class StateConflict(PersistenceError):
    """Compare-and-swap failed: the run is no longer in the expected state."""

    def __init__(self, run_id: str, expected_state: str, actual_state: str) -> None:
        self.run_id = run_id
        self.expected_state = expected_state
        self.actual_state = actual_state
        super().__init__(
            f"run {run_id!r} is in state {actual_state!r}, expected {expected_state!r}"
        )


class UnknownBudgetField(PersistenceError, ValueError):
    """A budget update names a column that is not a run counter."""
