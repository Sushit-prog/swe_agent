"""Typed repository API for the central SQLite audit database."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from sqlalchemy import func
from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from swe_agent.persistence.database import (
    DEFAULT_BUSY_TIMEOUT_MS,
    create_engine_for_path,
    initialize_database,
    session_scope,
)
from swe_agent.persistence.errors import (
    RunAlreadyExists,
    RunNotFound,
    StateConflict,
    UnknownBudgetField,
)
from swe_agent.persistence.models import Event, Run, StateTransition
from swe_agent.tracing.logging import redact_text

MAX_PAYLOAD_BYTES = 64 * 1024
TRUNCATION_MARKER = "[PAYLOAD_TRUNCATED]"
BUDGET_FIELDS = frozenset(
    {
        "debug_attempts_used",
        "replan_attempts_used",
        "rework_cycles_used",
        "recovery_attempts_used",
        "step_index",
    }
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _prepare_payload(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    redacted = redact_text(raw)
    if len(redacted.encode("utf-8")) <= MAX_PAYLOAD_BYTES:
        return redacted
    fallback: dict[str, Any] = {
        "_truncated": True,
        "marker": TRUNCATION_MARKER,
        "original_bytes": len(raw.encode("utf-8")),
        "preview": redacted[: MAX_PAYLOAD_BYTES // 4],
    }
    result = json.dumps(fallback, sort_keys=True, separators=(",", ":"))
    if len(result.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        fallback["preview"] = ""
        result = json.dumps(fallback, sort_keys=True, separators=(",", ":"))
    return result


class Repository:
    """Transactional, typed access to runs, transitions, and events."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    @classmethod
    def open(
        cls, db_path: Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
    ) -> Repository:
        engine, _ = create_engine_for_path(db_path, busy_timeout_ms=busy_timeout_ms)
        initialize_database(engine, db_path.expanduser().resolve())
        return cls(engine)

    @contextmanager
    def transaction(self) -> Iterator[Session]:
        with session_scope(self.engine) as session:
            yield session

    def create_run(self, run_id: str, issue_url: str) -> Run:
        with self.transaction() as session:
            if session.get(Run, run_id) is not None:
                raise RunAlreadyExists(f"run {run_id!r} already exists")
            now = _utcnow()
            run = Run(
                id=run_id,
                issue_url=issue_url,
                state="CREATED",
                created_at=now,
                updated_at=now,
            )
            session.add(run)
            session.flush()
        run.created_at = _aware(run.created_at)
        run.updated_at = _aware(run.updated_at)
        return run

    def get_run(self, run_id: str) -> Run | None:
        with Session(self.engine) as session:
            run = session.get(Run, run_id)
            if run is None:
                return None
            created_at = run.created_at
            updated_at = run.updated_at
        run.created_at = _aware(created_at)
        run.updated_at = _aware(updated_at)
        return run

    def apply_transition(
        self,
        run_id: str,
        expected_state: str,
        to_state: str,
        event: str,
        reason: str,
        budget_updates: dict[str, int],
    ) -> StateTransition:
        unknown = set(budget_updates) - BUDGET_FIELDS
        if unknown:
            raise UnknownBudgetField(f"unknown budget fields: {sorted(unknown)}")
        for name, value in budget_updates.items():
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative int")

        with self.transaction() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunNotFound(f"run {run_id!r} not found")
            if run.state != expected_state:
                raise StateConflict(run_id, expected_state, run.state)
            run.state = to_state
            run.updated_at = _utcnow()
            for name, value in budget_updates.items():
                setattr(run, name, value)
            row = StateTransition(
                run_id=run_id,
                from_state=expected_state,
                to_state=to_state,
                event=event,
                reason=redact_text(reason),
                ts=_utcnow(),
            )
            session.add(row)
            session.flush()
        row.ts = _aware(row.ts)
        return row

    def set_plan_progress(self, run_id: str, steps_total: int) -> Run:
        with self.transaction() as session:
            run = session.get(Run, run_id)
            if run is None:
                raise RunNotFound(f"run {run_id!r} not found")
            run.steps_total = steps_total
            run.step_index = 0
            run.debug_attempts_used = 0
            run.updated_at = _utcnow()
            session.flush()
        run.created_at = _aware(run.created_at)
        run.updated_at = _aware(run.updated_at)
        return run

    def append_event(
        self, run_id: str, actor: str, kind: str, payload: dict[str, Any]
    ) -> Event:
        payload_json = _prepare_payload(payload)
        with self.transaction() as session:
            if session.get(Run, run_id) is None:
                raise RunNotFound(f"run {run_id!r} not found")
            last_seq = session.exec(
                select(func.coalesce(func.max(Event.seq), 0)).where(Event.run_id == run_id)
            ).one()
            row = Event(
                run_id=run_id,
                seq=int(last_seq) + 1,
                ts=_utcnow(),
                actor=actor,
                kind=kind,
                payload_json=payload_json,
            )
            session.add(row)
            session.flush()
        row.ts = _aware(row.ts)
        return row

    def list_events(self, run_id: str) -> list[Event]:
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(Event).where(Event.run_id == run_id).order_by(Event.seq)
                )
            )
        for row in rows:
            row.ts = _aware(row.ts)
        return rows

    def list_transitions(self, run_id: str) -> list[StateTransition]:
        with Session(self.engine) as session:
            rows = list(
                session.exec(
                    select(StateTransition)
                    .where(StateTransition.run_id == run_id)
                    .order_by(StateTransition.id)
                )
            )
        for row in rows:
            row.ts = _aware(row.ts)
        return rows


def create_repository(
    db_path: Path | str, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS
) -> Repository:
    return Repository.open(Path(db_path), busy_timeout_ms=busy_timeout_ms)
