from pathlib import Path

import pytest

from swe_agent.persistence import Repository, create_repository


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "swe.db"


@pytest.fixture
def repo(db_path: Path) -> Repository:
    return create_repository(db_path)
