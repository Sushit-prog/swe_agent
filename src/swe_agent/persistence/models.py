"""SQLModel tables for the central SQLite audit database."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel

_ZERO = {"server_default": "0"}


class Run(SQLModel, table=True):
    __tablename__ = "runs"

    id: str = Field(primary_key=True)
    issue_url: str
    state: str
    created_at: datetime
    updated_at: datetime
    debug_attempts_used: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)
    replan_attempts_used: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)
    rework_cycles_used: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)
    recovery_attempts_used: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)
    step_index: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)
    steps_total: int = Field(default=0, nullable=False, sa_column_kwargs=_ZERO)


class StateTransition(SQLModel, table=True):
    __tablename__ = "state_transitions"

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", nullable=False)
    from_state: str
    to_state: str
    event: str
    reason: str
    ts: datetime


class Event(SQLModel, table=True):
    __tablename__ = "events"
    __table_args__ = (UniqueConstraint("run_id", "seq", name="uq_events_run_seq"),)

    id: int | None = Field(default=None, primary_key=True)
    run_id: str = Field(foreign_key="runs.id", nullable=False)
    seq: int
    ts: datetime
    actor: str
    kind: str
    payload_json: str
