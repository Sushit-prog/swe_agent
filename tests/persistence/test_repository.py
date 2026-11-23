import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from swe_agent.config.schema import SweAgentConfig
from swe_agent.persistence import (
    BUDGET_FIELDS,
    MAX_PAYLOAD_BYTES,
    TRUNCATION_MARKER,
    Event,
    Repository,
    Run,
    RunAlreadyExists,
    RunNotFound,
    SchemaVersionError,
    StateConflict,
    StateTransition,
    UnknownBudgetField,
    create_engine_for_path,
    create_repository,
)
from swe_agent.persistence import repository as repository_module

ISSUE = "https://github.com/o/r/issues/1"


def test_create_get_round_trip(repo: Repository, db_path: Path) -> None:
    run = repo.create_run("r1", ISSUE)
    assert run.id == "r1"
    assert run.issue_url == ISSUE
    assert run.state == "CREATED"
    assert run.created_at.utcoffset() == timedelta(0)
    assert run.updated_at.utcoffset() == timedelta(0)
    assert all(getattr(run, name) == 0 for name in BUDGET_FIELDS - {"step_index"})
    assert run.step_index == 0 and run.steps_total == 0

    fetched = repo.get_run("r1")
    assert fetched is not None
    assert fetched.id == run.id
    assert fetched.created_at == run.created_at
    assert repo.get_run("missing") is None

    if os.name == "posix":
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    else:
        assert db_path.exists()


def test_counter_columns_and_schema_shape(repo: Repository, db_path: Path) -> None:
    engine, _ = create_engine_for_path(db_path)
    with engine.connect() as conn:
        cols = {row[1]: (row[3], row[4]) for row in conn.exec_driver_sql("PRAGMA table_info(runs)")}
        transitions = {
            row[1] for row in conn.exec_driver_sql("PRAGMA table_info(state_transitions)")
        }
        events = {row[1] for row in conn.exec_driver_sql("PRAGMA table_info(events)")}
        version = conn.exec_driver_sql("PRAGMA user_version").scalar_one()
        foreign_keys = conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one()
        journal_mode = conn.exec_driver_sql("PRAGMA journal_mode").scalar_one()
        busy_timeout = conn.exec_driver_sql("PRAGMA busy_timeout").scalar_one()

    for name in BUDGET_FIELDS | {"steps_total"}:
        not_null, default = cols[name]
        assert not_null == 1
        assert default is not None
    assert "event" in transitions
    assert {"id", "run_id", "seq", "ts", "actor", "kind", "payload_json"} <= events
    assert version == 1
    assert foreign_keys == 1
    assert str(journal_mode).lower() == "wal"
    assert busy_timeout == 5_000


def test_apply_transition_cas_and_atomic_rollback(
    repo: Repository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.create_run("r1", ISSUE)
    row = repo.apply_transition(
        "r1", "CREATED", "PLANNING", "START_PLANNING", "ok", {"step_index": 2}
    )
    assert isinstance(row, StateTransition)
    assert row.event == "START_PLANNING"
    assert row.from_state == "CREATED"
    assert row.to_state == "PLANNING"
    assert row.ts.utcoffset() == timedelta(0)
    assert repo.get_run("r1").state == "PLANNING"
    assert repo.get_run("r1").step_index == 2
    assert len(repo.list_transitions("r1")) == 1

    with pytest.raises(StateConflict):
        repo.apply_transition("r1", "CREATED", "FAILED", "STALE", "stale", {})
    assert repo.get_run("r1").state == "PLANNING"
    assert len(repo.list_transitions("r1")) == 1

    original = repository_module.redact_text

    def boom(text: str) -> str:
        if "BOOM" in text:
            raise RuntimeError("boom")
        return original(text)

    monkeypatch.setattr(repository_module, "redact_text", boom)
    with pytest.raises(RuntimeError, match="boom"):
        repo.apply_transition("r1", "PLANNING", "EXECUTING", "PICK", "BOOM", {"step_index": 7})
    assert repo.get_run("r1").state == "PLANNING"
    assert repo.get_run("r1").step_index == 2
    assert len(repo.list_transitions("r1")) == 1


def test_apply_transition_validates_budget_updates(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    with pytest.raises(UnknownBudgetField):
        repo.apply_transition("r1", "CREATED", "PLANNING", "E", "r", {"bogus": 1})
    with pytest.raises(ValueError):
        repo.apply_transition("r1", "CREATED", "PLANNING", "E", "r", {"step_index": -1})
    with pytest.raises(RunNotFound):
        repo.apply_transition("missing", "CREATED", "PLANNING", "E", "r", {})


def test_budget_fields_are_exact() -> None:
    assert BUDGET_FIELDS == {
        "debug_attempts_used",
        "replan_attempts_used",
        "rework_cycles_used",
        "recovery_attempts_used",
        "step_index",
    }


def test_set_plan_progress(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    repo.apply_transition(
        "r1",
        "CREATED",
        "PLANNING",
        "START",
        "ok",
        {"step_index": 3, "debug_attempts_used": 4, "rework_cycles_used": 2},
    )
    run = repo.set_plan_progress("r1", 9)
    assert run.steps_total == 9
    assert run.step_index == 0
    assert run.debug_attempts_used == 0
    assert run.rework_cycles_used == 2
    with pytest.raises(RunNotFound):
        repo.set_plan_progress("missing", 1)


def test_reason_redacted(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    row = repo.apply_transition(
        "r1",
        "CREATED",
        "PLANNING",
        "START",
        "key sk-ant-ABCDEFGH1234",
        {},
    )
    assert "sk-ant-ABCDEFGH1234" not in row.reason
    assert "[REDACTED]" in row.reason


def test_append_event_seq_and_redaction(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    repo.create_run("r2", ISSUE)
    for i in range(3):
        event = repo.append_event("r1", "cli", "note", {"i": i})
        assert event.seq == i + 1
        assert event.ts.utcoffset() == timedelta(0)
    assert [e.seq for e in repo.list_events("r1")] == [1, 2, 3]
    assert repo.append_event("r2", "cli", "note", {}).seq == 1

    secret = "sk-ant-ABCDEFGH1234"
    event = repo.append_event("r1", "cli", "note", {"token": secret, "note": "hello"})
    stored = event.payload_json
    assert secret not in stored
    assert "[REDACTED]" in stored
    assert json.loads(stored)["note"] == "hello"


def test_append_event_payload_cap(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    event = repo.append_event("r1", "cli", "note", {"blob": "x" * 100_000})
    assert len(event.payload_json.encode("utf-8")) <= MAX_PAYLOAD_BYTES
    assert TRUNCATION_MARKER in event.payload_json
    parsed = json.loads(event.payload_json)
    assert parsed["_truncated"] is True
    assert parsed["marker"] == TRUNCATION_MARKER


def test_append_only_triggers(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    event = repo.append_event("r1", "cli", "note", {"v": 1})
    transition = repo.apply_transition("r1", "CREATED", "PLANNING", "START", "ok", {})

    with pytest.raises(IntegrityError):
        with repo.transaction() as session:
            session.get(Event, event.id).kind = "tampered"
    with pytest.raises(IntegrityError):
        with repo.transaction() as session:
            session.delete(session.get(Event, event.id))
    with pytest.raises(IntegrityError):
        with repo.transaction() as session:
            session.get(StateTransition, transition.id).to_state = "FAILED"
    with pytest.raises(IntegrityError):
        with repo.transaction() as session:
            session.delete(session.get(StateTransition, transition.id))

    assert repo.list_events("r1")[0].kind == "note"
    assert repo.list_transitions("r1")[0].to_state == "PLANNING"


def test_foreign_keys_enforced(repo: Repository) -> None:
    with pytest.raises(IntegrityError):
        with repo.transaction() as session:
            session.add(
                Event(
                    run_id="missing",
                    seq=1,
                    ts=datetime.now(timezone.utc),
                    actor="cli",
                    kind="note",
                    payload_json="{}",
                )
            )


def test_no_update_or_delete_api(repo: Repository) -> None:
    public = {name for name in dir(repo) if not name.startswith("_")}
    assert not any(name.startswith(("update_", "delete_", "remove_")) for name in public)


def test_duplicate_run(repo: Repository) -> None:
    repo.create_run("r1", ISSUE)
    with pytest.raises(RunAlreadyExists):
        repo.create_run("r1", ISSUE)


def test_schema_version_mismatch(db_path: Path) -> None:
    create_repository(db_path)
    engine, _ = create_engine_for_path(db_path)
    with engine.begin() as conn:
        conn.exec_driver_sql("PRAGMA user_version = 99")
    with pytest.raises(SchemaVersionError, match="delete the dev DB"):
        create_repository(db_path)


def test_database_config_expands_and_overrides(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("SWE_DATABASE__PATH", str(tmp_path / "override.db"))
    config = SweAgentConfig()
    assert config.database.path == tmp_path / "override.db"
    assert SweAgentConfig(database={"path": "~/x.db"}).database.path == Path.home() / "x.db"


def test_swe_clean_never_touches_db_rows(tmp_path: Path) -> None:
    from swe_agent.github.urls import IssueRef
    from swe_agent.workspace import manager

    db = tmp_path / "state" / "swe.db"
    repo = create_repository(db)
    repo.create_run("survivor", ISSUE)
    repo.append_event("survivor", "test", "note", {"v": 1})

    config = SweAgentConfig(
        workspace={"root": str(tmp_path / "home")},
        database={"path": str(db)},
    )
    workspace = manager.create_run(IssueRef(owner="o", repo="r", number=1), config)
    manager.cleanup_run(workspace.run_id, config)

    assert db.exists()
    assert repo.get_run("survivor") is not None
    assert len(repo.list_events("survivor")) == 1
