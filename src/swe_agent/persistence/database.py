"""SQLite engine setup, pragmas, triggers, and schema versioning."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine

from swe_agent.persistence.errors import PersistenceError, SchemaVersionError

SCHEMA_VERSION = 1
DEFAULT_BUSY_TIMEOUT_MS = 5_000

_TRIGGERS = (
    "CREATE TRIGGER IF NOT EXISTS events_no_update "
    "BEFORE UPDATE ON events BEGIN "
    "SELECT RAISE(ABORT, 'events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS events_no_delete "
    "BEFORE DELETE ON events BEGIN "
    "SELECT RAISE(ABORT, 'events is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS state_transitions_no_update "
    "BEFORE UPDATE ON state_transitions BEGIN "
    "SELECT RAISE(ABORT, 'state_transitions is append-only'); END",
    "CREATE TRIGGER IF NOT EXISTS state_transitions_no_delete "
    "BEFORE DELETE ON state_transitions BEGIN "
    "SELECT RAISE(ABORT, 'state_transitions is append-only'); END",
)


def _prepare_path(db_path: Path) -> Path:
    path = db_path.expanduser().resolve()
    if path.is_symlink():
        raise PersistenceError(f"database path must not be a symlink: {path}")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    return path


def create_engine_for_path(
    db_path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> tuple[Engine, Path]:
    path = _prepare_path(db_path)
    engine = create_engine(
        f"sqlite:///{path.as_posix()}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: object, _connection_record: object) -> None:
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        finally:
            cursor.close()

    @event.listens_for(engine, "begin")
    def _begin_immediate(conn: object) -> None:
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine, path


def initialize_database(engine: Engine, path: Path) -> None:
    with engine.connect() as conn:
        version = int(conn.exec_driver_sql("PRAGMA user_version").scalar_one())

    if version not in (0, SCHEMA_VERSION):
        raise SchemaVersionError(
            f"delete the dev DB at {path}: schema version {version}, "
            f"expected {SCHEMA_VERSION}"
        )

    if version == 0:
        SQLModel.metadata.create_all(engine)

    with engine.begin() as conn:
        for statement in _TRIGGERS:
            conn.exec_driver_sql(statement)
        conn.exec_driver_sql(f"PRAGMA user_version = {SCHEMA_VERSION}")


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    with Session(engine, expire_on_commit=False) as session:
        try:
            yield session
            session.commit()
        except BaseException:
            session.rollback()
            raise
