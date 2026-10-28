"""Shared fixture: local bare git repositories built with the real git binary.

No network. Used by workspace, tools, security, and e2e tests.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path, env: dict | None = None) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
        shell=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {proc.stderr}")
    return proc.stdout


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """A non-bare working repo with one commit (cloneable source)."""
    repo = tmp_path / "src-repo"
    repo.mkdir()
    _git(["init", "-b", "main"], repo)
    _git(["config", "user.email", "test@example.com"], repo)
    _git(["config", "user.name", "Test"], repo)
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _git(["add", "hello.txt"], repo)
    _git(["commit", "-m", "initial"], repo)
    return repo


@pytest.fixture
def bare_repo(tmp_path: Path, sample_repo: Path) -> Path:
    """A bare clone of sample_repo — the clone source for tests."""
    bare = tmp_path / "remote.git"
    _git(["clone", "--bare", str(sample_repo), str(bare)], tmp_path)
    return bare


def make_repo_with_marker(path: Path, marker_name: str = "marker.txt") -> Path:
    """Create a repo containing a file tests can assert was (not) modified."""
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)
    (path / marker_name).write_text("base\n", encoding="utf-8")
    _git(["add", marker_name], cwd=path)
    _git(["commit", "-m", "init"], path)
    return path
