import threading
import time
from pathlib import Path

import pytest
from sqlalchemy.exc import OperationalError

from swe_agent.persistence import Repository, Run, StateConflict, create_repository

ISSUE = "https://github.com/o/r/issues/1"


def _both(db_path: Path) -> tuple[Repository, Repository]:
    return create_repository(db_path), create_repository(db_path)


def test_two_racing_transitions_exactly_one_wins(db_path: Path) -> None:
    first, second = _both(db_path)
    first.create_run("race", ISSUE)
    barrier = threading.Barrier(2, timeout=10)
    winners: list[int] = []
    losers: list[str] = []

    def worker(index: int, repo: Repository) -> None:
        barrier.wait()
        try:
            repo.apply_transition("race", "CREATED", "PLANNING", "START", f"w{index}", {})
            winners.append(index)
        except StateConflict:
            losers.append(index)
        except Exception as exc:
            losers.append(type(exc).__name__)

    threads = [
        threading.Thread(target=worker, args=(0, first)),
        threading.Thread(target=worker, args=(1, second)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert len(winners) == 1
    assert len(losers) == 1
    assert first.get_run("race").state == "PLANNING"
    assert len(first.list_transitions("race")) == 1


def test_begin_immediate_blocks_second_writer(db_path: Path) -> None:
    holder_repo, _ = _both(db_path)
    contender_repo = create_repository(db_path, busy_timeout_ms=200)
    holder_repo.create_run("lock", ISSUE)

    holding = threading.Event()
    release = threading.Event()
    outcomes: dict[str, object] = {}

    def holder() -> None:
        try:
            with holder_repo.transaction() as session:
                run = session.get(Run, "lock")
                run.state = "PLANNING"
                session.flush()
                holding.set()
                release.wait(10)
        except Exception as exc:
            outcomes["holder_error"] = repr(exc)

    def contender() -> None:
        assert holding.wait(10)
        start = time.monotonic()
        try:
            contender_repo.apply_transition(
                "lock", "CREATED", "FAILED", "FATAL", "should not commit", {}
            )
            outcomes["result"] = "committed"
        except OperationalError:
            outcomes["result"] = "locked"
        except Exception as exc:
            outcomes["result"] = f"error:{type(exc).__name__}"
        outcomes["elapsed"] = time.monotonic() - start

    threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
    for thread in threads:
        thread.start()
    assert holding.wait(10)
    time.sleep(0.35)
    release.set()
    for thread in threads:
        thread.join(timeout=15)

    assert "holder_error" not in outcomes
    assert outcomes["result"] == "locked"
    assert float(outcomes["elapsed"]) >= 0.15
    assert holder_repo.get_run("lock").state == "PLANNING"
    assert len(holder_repo.list_transitions("lock")) == 0
