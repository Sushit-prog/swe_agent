"""Persistence package: central SQLite database and typed repository API."""

from swe_agent.persistence.database import SCHEMA_VERSION, create_engine_for_path
from swe_agent.persistence.errors import (
    PersistenceError,
    RunAlreadyExists,
    RunNotFound,
    SchemaVersionError,
    StateConflict,
    UnknownBudgetField,
)
from swe_agent.persistence.models import Event, Run, StateTransition
from swe_agent.persistence.repository import (
    BUDGET_FIELDS,
    MAX_PAYLOAD_BYTES,
    TRUNCATION_MARKER,
    Repository,
    create_repository,
)

__all__ = [
    "BUDGET_FIELDS",
    "MAX_PAYLOAD_BYTES",
    "SCHEMA_VERSION",
    "TRUNCATION_MARKER",
    "Event",
    "PersistenceError",
    "Repository",
    "Run",
    "RunAlreadyExists",
    "RunNotFound",
    "SchemaVersionError",
    "StateConflict",
    "StateTransition",
    "UnknownBudgetField",
    "create_engine_for_path",
    "create_repository",
]
